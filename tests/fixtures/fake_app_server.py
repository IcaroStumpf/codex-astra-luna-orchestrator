"""Deterministic JSONL app-server stand-in used by transport unit tests."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any


_stdout_lock = threading.Lock()
_state_lock = threading.Lock()
_server_responses: list[dict[str, Any]] = []
_workers: list[threading.Thread] = []


def send(message: dict[str, Any]) -> None:
    payload = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
    with _stdout_lock:
        sys.stdout.write(payload + "\n")
        sys.stdout.flush()


def reply_later(request_id: int, params: dict[str, Any]) -> None:
    time.sleep(float(params.get("delay", 0)))
    send(
        {
            "id": request_id,
            "result": {
                "label": params.get("label"),
                "delay": float(params.get("delay", 0)),
            },
        }
    )


def _trace(path: Path | None, message: dict[str, Any]) -> None:
    if path is None:
        return
    with path.open("a", encoding="utf-8") as trace:
        trace.write(json.dumps(message, ensure_ascii=False) + "\n")


def _orchestration_server(args: list[str]) -> None:
    trace_path = None
    if "--trace" in args:
        trace_path = Path(args[args.index("--trace") + 1])
    hold_for_approval = "--approval" in args
    exit_on_turn_start = "--exit-on-turn-start" in args
    threads = 0
    turns = 0
    active_turns: dict[str, str] = {}

    for raw_line in sys.stdin.buffer:
        request = json.loads(raw_line.decode("utf-8"))
        method = request.get("method")
        request_id = request.get("id")
        params = request.get("params") or {}
        _trace(trace_path, request)

        if method == "initialize":
            expected = {
                "clientInfo": {"name": "codex_orchestrator", "version": "0.3.0"},
                "capabilities": {"experimentalApi": True},
            }
            if params != expected:
                send({"id": request_id, "error": {"code": -32602, "message": "unexpected initialize params"}})
            else:
                send({"id": request_id, "result": {"userAgent": "fixture-orchestrator"}})
        elif method == "initialized":
            continue
        elif method == "model/list":
            send({"id": request_id, "result": {"data": [{"id": "fixture-model"}], "nextCursor": None}})
        elif method == "thread/start":
            threads += 1
            thread_id = f"fixture-thread-{threads}"
            send({"id": request_id, "result": {"thread": {"id": thread_id}}})
            send({"method": "thread/started", "params": {"thread": {"id": thread_id, "model": params.get("model")}}})
        elif method == "thread/resume":
            thread_id = params.get("threadId")
            send({"id": request_id, "result": {"thread": {"id": thread_id}}})
        elif method == "turn/start":
            if exit_on_turn_start:
                os._exit(7)
            turns += 1
            thread_id = params["threadId"]
            turn_id = f"fixture-turn-{turns}"
            active_turns[thread_id] = turn_id
            send({"method": "turn/started", "params": {
                "threadId": thread_id, "turn": {"id": turn_id, "status": "inProgress"},
            }})
            send({"id": request_id, "result": {"turn": {"id": turn_id, "status": "inProgress"}}})
            if hold_for_approval:
                send({"id": "fixture-approval-1", "method": "item/commandExecution/requestApproval", "params": {
                    "threadId": thread_id, "turnId": turn_id, "command": ["fixture", "approval"],
                }})
            else:
                result = f"fixture result for {turn_id}"
                send({"method": "item/completed", "params": {
                    "threadId": thread_id, "turnId": turn_id,
                    "item": {"id": f"message-{turn_id}", "type": "agentMessage", "phase": "final_answer", "text": result},
                }})
                send({"method": "turn/completed", "params": {
                    "threadId": thread_id, "turn": {"id": turn_id, "status": "completed"},
                }})
        elif method == "turn/settings/update":
            send({"id": request_id, "result": {"status": "applied"}})
        elif method == "turn/interrupt":
            send({"id": request_id, "result": {}})
        elif method is None and "id" in request and ("result" in request or "error" in request):
            # This is a response to the approval request sent above.
            response = request.get("result") or {}
            thread_id = params.get("threadId")
            if thread_id is None and active_turns:
                thread_id = next(iter(active_turns))
            turn_id = active_turns.pop(thread_id, None)
            _trace(trace_path, {"method": "fixture/approval_response", "response": response})
            if turn_id:
                result = f"fixture approval result: {response.get('decision', 'none')}"
                send({"method": "item/completed", "params": {
                    "threadId": thread_id, "turnId": turn_id,
                    "item": {"id": f"message-{turn_id}", "type": "agentMessage", "phase": "final_answer", "text": result},
                }})
                send({"method": "turn/completed", "params": {
                    "threadId": thread_id, "turn": {"id": turn_id, "status": "completed"},
                }})
        else:
            send({"id": request_id, "error": {"code": -32601, "message": f"unknown fixture method: {method}"}})


def main() -> None:
    if "--orchestration" in sys.argv[1:]:
        _orchestration_server(sys.argv[1:])
        return

    for raw_line in sys.stdin.buffer:
        request = json.loads(raw_line.decode("utf-8"))
        method = request.get("method")
        request_id = request.get("id")
        params = request.get("params") or {}

        if method is None and "id" in request and ("result" in request or "error" in request):
            with _state_lock:
                _server_responses.append(request)
            continue

        if method == "initialize":
            expected = {
                "clientInfo": {"name": "codex_orchestrator", "version": "0.3.0"},
                "capabilities": {"experimentalApi": True},
            }
            if params != expected:
                send(
                    {
                        "id": request_id,
                        "error": {"code": -32602, "message": "unexpected initialize params"},
                    }
                )
            else:
                send({"id": request_id, "result": {"userAgent": "fake-app-server"}})
        elif method == "initialized":
            continue
        elif method == "model/list":
            send(
                {
                    "id": request_id,
                    "result": {"models": [{"id": "fake-model", "displayName": "Fake"}]},
                }
            )
        elif method == "event/before":
            send({"method": "turn/started", "params": {"turn": {"id": "turn-1"}}})
            send({"id": request_id, "result": {"ok": True}})
        elif method == "server/request":
            send(
                {
                    "id": "approval-1",
                    "method": "item/commandExecution/requestApproval",
                    "params": {"command": ["git", "status"]},
                }
            )
            send({"id": request_id, "result": {"requested": True}})
        elif method == "server/responses":
            with _state_lock:
                responses = list(_server_responses)
            send({"id": request_id, "result": responses})
        elif method == "error":
            send(
                {
                    "id": request_id,
                    "error": {"code": 409, "message": "fixture rejection", "data": {"retry": False}},
                }
            )
        elif method == "delay":
            worker = threading.Thread(target=reply_later, args=(request_id, params), daemon=True)
            _workers.append(worker)
            worker.start()
        elif method == "never":
            continue
        elif method == "exit":
            sys.stdout.flush()
            os._exit(7)
        elif method == "malformed":
            with _stdout_lock:
                sys.stdout.write("this is not JSON\n")
                sys.stdout.flush()
        elif method == "overlong":
            limit = int(params.get("limit", 4 * 1024 * 1024))
            with _stdout_lock:
                sys.stdout.buffer.write(b"x" * (limit + 1) + b"\n")
                sys.stdout.flush()
        elif method == "stderr":
            sys.stderr.write("diagnostic-" + ("x" * 200_000))
            sys.stderr.flush()
            send({"id": request_id, "result": {"written": True}})
        elif "id" in request:
            send({"id": request_id, "result": {"method": method, "params": params}})
        else:
            continue

    for worker in _workers:
        worker.join(timeout=0.5)


if __name__ == "__main__":
    main()
