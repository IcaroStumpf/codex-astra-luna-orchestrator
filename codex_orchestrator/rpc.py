"""Synchronous JSONL transport for the Codex app-server protocol.

The app-server speaks JSON-RPC-shaped messages over newline-delimited JSON on
stdio. A dedicated reader thread routes replies to their waiting callers while
leaving notifications and server-initiated requests available to the owner.
"""

from __future__ import annotations

import json
import math
import os
import queue
import shutil
import subprocess
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import __version__


_MAX_LINE_BYTES = 4 * 1024 * 1024
_STDERR_TAIL_BYTES = 64 * 1024
_MAX_QUEUED_EVENTS = 1024
_MAX_PENDING_REQUESTS = 4096
_SHUTDOWN_WAIT_SECONDS = 1.0


class RpcError(RuntimeError):
    """Raised for transport failures and app-server RPC errors."""

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        data: Any = None,
        error: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.data = data
        self.error = dict(error) if error is not None else None


class _PendingRequest:
    __slots__ = ("event", "result", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: Any = None
        self.error: RpcError | None = None


class AppServer:
    """Own a Codex app-server child and provide blocking RPC calls.

    ``request`` blocks only its caller. One reader thread continuously consumes
    stdout so concurrent requests, notifications, and server-initiated requests
    can share the same JSONL connection safely.
    """

    def __init__(
        self,
        command: list[str] | None = None,
        cwd: str | Path | None = None,
        request_timeout: float = 30,
    ) -> None:
        if request_timeout < 0 or not math.isfinite(request_timeout):
            raise ValueError("request_timeout must be finite and non-negative")
        self.request_timeout = float(request_timeout)
        self.cwd = os.fspath(cwd) if cwd is not None else None
        self._command = list(command) if command is not None else None
        if self._command is not None and not self._command:
            raise ValueError("command must contain an executable")

        self._process: subprocess.Popen[bytes] | None = None
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._events: queue.Queue[dict[str, Any]] = queue.Queue(
            maxsize=_MAX_QUEUED_EVENTS
        )
        self._pending: dict[int, _PendingRequest] = {}
        self._next_request_id = 1
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._stderr_lock = threading.Lock()
        self._stderr_tail_bytes = bytearray()
        self._channel_error: RpcError | None = None
        self._started = False
        self._initialized = False
        self._closing = False
        self._closed = False

    @property
    def alive(self) -> bool:
        """Whether the child process is still running."""

        process = self._process
        return process is not None and process.poll() is None

    @property
    def stderr_tail(self) -> str:
        """The most recent bounded stderr output, decoded with replacement."""

        with self._stderr_lock:
            return bytes(self._stderr_tail_bytes).decode("utf-8", errors="replace")

    def start(self) -> AppServer:
        """Start the child and complete the required initialize handshake."""

        with self._lifecycle_lock:
            with self._state_lock:
                if self._closed:
                    raise RpcError("app-server client is closed")
                if self._initialized:
                    return self
                if self._started:
                    raise RpcError("app-server startup is already in progress")

            command = self._command or self._default_command()
            self._validate_executable(command[0])
            with self._state_lock:
                self._started = True
            try:
                self._process = subprocess.Popen(
                    command,
                    cwd=self.cwd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                    close_fds=True,
                )
            except FileNotFoundError as exc:
                with self._state_lock:
                    self._started = False
                raise RpcError(
                    f"could not start app-server executable {command[0]!r}: {exc}"
                ) from exc
            except OSError as exc:
                with self._state_lock:
                    self._started = False
                raise RpcError(f"could not start app-server: {exc}") from exc

            self._reader_thread = threading.Thread(
                target=self._read_stdout,
                name="codex-app-server-reader",
                daemon=True,
            )
            self._stderr_thread = threading.Thread(
                target=self._read_stderr,
                name="codex-app-server-stderr",
                daemon=True,
            )
            self._reader_thread.start()
            self._stderr_thread.start()

            try:
                self._request_internal(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "codex_orchestrator",
                            "version": __version__,
                        },
                        "capabilities": {"experimentalApi": True},
                    },
                    timeout=self.request_timeout,
                    allow_uninitialized=True,
                )
                self._notify_internal("initialized", {}, allow_uninitialized=True)
                with self._state_lock:
                    if self._channel_error is not None:
                        raise self._channel_error
                    self._initialized = True
            except Exception:
                self._close_locked()
                raise
            return self

    def request(
        self,
        method: str,
        params: Any = None,
        timeout: float | None = None,
    ) -> Any:
        """Send an RPC request and return its result, or raise ``RpcError``."""

        return self._request_internal(
            method,
            params,
            timeout=self.request_timeout if timeout is None else timeout,
            allow_uninitialized=False,
        )

    def notify(self, method: str, params: Any = None) -> None:
        """Send a notification without waiting for a reply."""

        self._notify_internal(method, params, allow_uninitialized=False)

    def respond(
        self,
        request_id: int | str,
        result: Any = None,
        error: Mapping[str, Any] | None = None,
    ) -> None:
        """Respond to a server-initiated request returned by ``next_event``."""

        if isinstance(request_id, bool) or not isinstance(request_id, (int, str)):
            raise ValueError("request_id must be an integer or string")
        if error is not None and not isinstance(error, Mapping):
            raise ValueError("error must be a mapping or None")
        if error is None:
            message = {"id": request_id, "result": result}
        else:
            message = {"id": request_id, "error": dict(error)}
        self._write_message(message, allow_uninitialized=False)

    def next_event(self, timeout: float | None = 0.1) -> dict[str, Any] | None:
        """Return the next notification or server request, or ``None`` on timeout.

        Incoming server-initiated requests are returned with their ``id`` intact
        so callers can approve, decline, or otherwise answer them with
        :meth:`respond`.
        """

        if timeout is not None and (timeout < 0 or not math.isfinite(timeout)):
            raise ValueError("timeout must be finite and non-negative, or None")
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            wait_for = 0.1 if deadline is None else max(0.0, min(0.1, deadline - time.monotonic()))
            try:
                return self._events.get(timeout=wait_for)
            except queue.Empty:
                with self._state_lock:
                    channel_error = self._channel_error
                    closed = self._closed or self._closing
                if channel_error is not None:
                    raise channel_error
                if closed or (deadline is not None and time.monotonic() >= deadline):
                    return None

    def close(self) -> None:
        """Stop the child with bounded waits and release its pipes and threads."""

        with self._lifecycle_lock:
            self._close_locked()

    def __enter__(self) -> AppServer:
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def _request_internal(
        self,
        method: str,
        params: Any,
        *,
        timeout: float,
        allow_uninitialized: bool,
    ) -> Any:
        if not isinstance(method, str) or not method:
            raise ValueError("method must be a non-empty string")
        if timeout < 0 or not math.isfinite(timeout):
            raise ValueError("timeout must be finite and non-negative")
        self._ensure_usable(allow_uninitialized=allow_uninitialized)

        with self._pending_lock:
            if len(self._pending) >= _MAX_PENDING_REQUESTS:
                raise RpcError("too many outstanding app-server requests")
            request_id = self._next_request_id
            self._next_request_id += 1
            pending = _PendingRequest()
            self._pending[request_id] = pending

        message: dict[str, Any] = {"method": method, "id": request_id}
        if params is not None:
            message["params"] = params
        try:
            self._write_message(message, allow_uninitialized=allow_uninitialized)
        except Exception:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise

        if not pending.event.wait(timeout):
            with self._pending_lock:
                if self._pending.get(request_id) is pending:
                    self._pending.pop(request_id, None)
                else:
                    # A reply or channel failure won the race with the timeout.
                    pending.event.wait(0)
            if not pending.event.is_set():
                raise RpcError(f"app-server request {method!r} timed out after {timeout:g}s")

        if pending.error is not None:
            raise pending.error
        return pending.result

    def _notify_internal(
        self,
        method: str,
        params: Any,
        *,
        allow_uninitialized: bool,
    ) -> None:
        if not isinstance(method, str) or not method:
            raise ValueError("method must be a non-empty string")
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        self._write_message(message, allow_uninitialized=allow_uninitialized)

    def _ensure_usable(self, *, allow_uninitialized: bool) -> None:
        with self._state_lock:
            if self._channel_error is not None:
                raise self._channel_error
            if self._closed or self._closing:
                raise RpcError("app-server client is closed")
            if not self._started or self._process is None:
                raise RpcError("app-server client has not been started")
            if not allow_uninitialized and not self._initialized:
                raise RpcError("app-server client is not initialized")
            process = self._process
        if process.poll() is not None:
            error = self._process_exit_error(process)
            self._fail_channel(error)
            raise error

    def _write_message(
        self, message: dict[str, Any], *, allow_uninitialized: bool
    ) -> None:
        self._ensure_usable(allow_uninitialized=allow_uninitialized)
        try:
            encoded = (
                json.dumps(
                    message,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
        except (TypeError, ValueError) as exc:
            raise RpcError(f"app-server message is not valid JSON: {exc}") from exc
        with self._write_lock:
            with self._state_lock:
                if self._closed or self._closing:
                    raise RpcError("app-server client is closed")
                process = self._process
                channel_error = self._channel_error
            if channel_error is not None:
                raise channel_error
            if process is None or process.stdin is None or process.poll() is not None:
                error = self._process_exit_error(process)
                self._fail_channel(error)
                raise error
            try:
                process.stdin.write(encoded)
                process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                error = RpcError(f"failed writing to app-server stdin: {exc}")
                self._fail_channel(error)
                raise error from exc

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            while True:
                raw_line = process.stdout.readline(_MAX_LINE_BYTES + 1)
                if not raw_line:
                    if not self._is_closing():
                        try:
                            process.wait(timeout=0.2)
                        except subprocess.TimeoutExpired:
                            pass
                    if not self._is_closing():
                        self._fail_channel(self._process_exit_error(process))
                    return
                if len(raw_line) > _MAX_LINE_BYTES or not raw_line.endswith(b"\n"):
                    raise RpcError(
                        f"app-server JSONL frame exceeds {_MAX_LINE_BYTES} bytes or is not LF-terminated"
                    )
                try:
                    line = raw_line[:-1].decode("utf-8", errors="strict")
                    message = json.loads(
                        line,
                        parse_constant=lambda value: (_ for _ in ()).throw(
                            ValueError(f"invalid JSON constant {value}")
                        ),
                    )
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
                    raise RpcError(f"malformed app-server JSONL frame: {exc}") from exc
                if not isinstance(message, dict):
                    raise RpcError("malformed app-server JSONL frame: expected a JSON object")
                self._route_message(message)
        except RpcError as exc:
            if not self._is_closing():
                self._fail_channel(exc)
        except (OSError, ValueError) as exc:
            if not self._is_closing():
                self._fail_channel(RpcError(f"app-server stdout read failed: {exc}"))

    def _route_message(self, message: dict[str, Any]) -> None:
        has_id = "id" in message
        has_method = "method" in message
        if has_method:
            method = message.get("method")
            if not isinstance(method, str) or not method:
                raise RpcError("malformed app-server message: method must be a non-empty string")
            if has_id:
                request_id = message["id"]
                if isinstance(request_id, bool) or not isinstance(request_id, (int, str)):
                    raise RpcError("malformed app-server request: id must be an integer or string")
            self._queue_event(message)
            return

        if not has_id or ("result" not in message and "error" not in message):
            raise RpcError("malformed app-server message: expected request, notification, or response")
        if "result" in message and "error" in message:
            raise RpcError("malformed app-server response: it contains both result and error")
        request_id = message["id"]
        if isinstance(request_id, bool) or not isinstance(request_id, (int, str)):
            raise RpcError("malformed app-server response: id must be an integer or string")
        response_error: RpcError | None = None
        if "error" in message:
            error = message["error"]
            if not isinstance(error, dict):
                raise RpcError("malformed app-server response: error must be an object")
            code = error.get("code")
            text = error.get("message")
            if isinstance(code, bool) or not isinstance(code, int) or not isinstance(text, str):
                raise RpcError(
                    "malformed app-server response: error needs integer code and string message"
                )
            response_error = RpcError(
                f"app-server request failed ({code}): {text}",
                code=code,
                data=error.get("data"),
                error=error,
            )

        with self._pending_lock:
            pending = self._pending.pop(request_id, None)
            if pending is None:
                # Timed-out requests may still receive a late reply. It no longer
                # has a waiter and must not interfere with later request IDs.
                return
            if response_error is not None:
                pending.error = response_error
            else:
                pending.result = message["result"]
            pending.event.set()

    def _queue_event(self, message: dict[str, Any]) -> None:
        try:
            self._events.put_nowait(message)
        except queue.Full:
            raise RpcError(
                f"app-server event queue is full ({_MAX_QUEUED_EVENTS} events); consumer is not keeping up"
            )

    def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            while True:
                chunk = process.stderr.read(4096)
                if not chunk:
                    return
                with self._stderr_lock:
                    self._stderr_tail_bytes.extend(chunk)
                    overflow = len(self._stderr_tail_bytes) - _STDERR_TAIL_BYTES
                    if overflow > 0:
                        del self._stderr_tail_bytes[:overflow]
        except (OSError, ValueError):
            return

    def _fail_channel(self, error: RpcError) -> None:
        with self._state_lock:
            if self._channel_error is None and not self._closing:
                self._channel_error = error
            stored_error = self._channel_error
            process = self._process
        if stored_error is None:
            return
        with self._pending_lock:
            for pending in self._pending.values():
                pending.error = stored_error
                pending.event.set()
            self._pending.clear()
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def _close_locked(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closing = True
            self._initialized = False
            process = self._process
        close_error = RpcError("app-server client is closed")
        with self._pending_lock:
            for pending in self._pending.values():
                pending.error = close_error
                pending.event.set()
            self._pending.clear()

        if process is not None:
            if process.stdin is not None:
                try:
                    with self._write_lock:
                        process.stdin.close()
                except OSError:
                    pass
            self._wait_for_process(process)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass

        current_thread = threading.current_thread()
        for thread in (self._reader_thread, self._stderr_thread):
            if thread is not None and thread is not current_thread:
                thread.join(timeout=_SHUTDOWN_WAIT_SECONDS)
        with self._state_lock:
            self._closed = True
            self._closing = False

    def _wait_for_process(self, process: subprocess.Popen[bytes]) -> None:
        try:
            process.wait(timeout=_SHUTDOWN_WAIT_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=_SHUTDOWN_WAIT_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=_SHUTDOWN_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            pass

    def _is_closing(self) -> bool:
        with self._state_lock:
            return self._closing or self._closed

    def _process_exit_error(self, process: subprocess.Popen[bytes] | None) -> RpcError:
        code = process.poll() if process is not None else None
        if process is None:
            message = "app-server stdout closed"
        elif code is None:
            message = "app-server stdout closed while the process remained running"
        else:
            message = f"app-server process exited (code {code})"
        tail = self.stderr_tail.strip()
        if tail:
            message = f"{message}; stderr tail: {tail}"
        return RpcError(message)

    @staticmethod
    def _default_command() -> list[str]:
        executable: str | None
        if os.name == "nt":
            executable = shutil.which("codex.exe") or shutil.which("codex")
        else:
            executable = shutil.which("codex")
        if executable is None:
            raise RpcError(
                "Codex CLI executable was not found on PATH; install Codex CLI or pass command=[...]."
            )
        AppServer._validate_executable(executable)
        return [executable, "app-server"]

    @staticmethod
    def _validate_executable(executable: str) -> None:
        if os.name != "nt":
            return
        suffix = Path(executable).suffix.casefold()
        if suffix in {".cmd", ".bat"}:
            raise RpcError(
                f"Codex CLI resolved to the Windows {suffix} wrapper {executable!r}; "
                "install a codex.exe executable or pass a native executable command. "
                "The app-server transport will not invoke command wrappers through a shell."
            )
