from __future__ import annotations

import concurrent.futures
import sys
import threading
import time
import unittest
from pathlib import Path

from codex_orchestrator.rpc import AppServer, RpcError


ROOT = Path(__file__).resolve().parents[1]
FAKE_APP_SERVER = ROOT / "tests" / "fixtures" / "fake_app_server.py"


def fake_server(*, request_timeout: float = 1.0) -> AppServer:
    return AppServer(
        command=[sys.executable, str(FAKE_APP_SERVER)],
        cwd=ROOT,
        request_timeout=request_timeout,
    )


def wait_until_dead(server: AppServer) -> None:
    deadline = time.monotonic() + 1
    while server.alive and time.monotonic() < deadline:
        time.sleep(0.005)


class AppServerTests(unittest.TestCase):
    def test_initialize_model_list_and_context_cleanup(self):
        server = fake_server()
        with server:
            self.assertTrue(server.alive)
            result = server.request("model/list")
            self.assertEqual(result["models"][0]["id"], "fake-model")
        self.assertFalse(server.alive)

    def test_event_arriving_before_response_is_queued(self):
        with fake_server() as server:
            self.assertEqual(server.request("event/before"), {"ok": True})
            event = server.next_event(timeout=0.1)
            self.assertEqual(event["method"], "turn/started")
            self.assertEqual(event["params"]["turn"]["id"], "turn-1")
            self.assertIsNone(server.next_event(timeout=0.01))

    def test_server_requests_keep_their_id_until_caller_responds(self):
        with fake_server() as server:
            self.assertEqual(server.request("server/request"), {"requested": True})
            request = server.next_event(timeout=0.1)
            self.assertEqual(request["id"], "approval-1")
            self.assertEqual(request["method"], "item/commandExecution/requestApproval")
            server.respond(request["id"], result={"decision": "decline"})
            responses = server.request("server/responses")
            self.assertEqual(
                responses,
                [{"id": "approval-1", "result": {"decision": "decline"}}],
            )

    def test_rpc_error_preserves_code_and_data(self):
        with fake_server() as server:
            with self.assertRaisesRegex(RpcError, "fixture rejection") as raised:
                server.request("error")
            self.assertEqual(raised.exception.code, 409)
            self.assertEqual(raised.exception.data, {"retry": False})

    def test_concurrent_requests_are_correlated_when_replies_arrive_out_of_order(self):
        with fake_server() as server:
            requests = [
                {"label": "first", "delay": 0.15},
                {"label": "second", "delay": 0.02},
                {"label": "third", "delay": 0.08},
            ]
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                futures = [executor.submit(server.request, "delay", params) for params in requests]
                results = [future.result(timeout=2) for future in futures]
            self.assertEqual([result["label"] for result in results], ["first", "second", "third"])

    def test_premature_eof_rejects_waiting_request(self):
        server = fake_server()
        server.start()
        try:
            with self.assertRaisesRegex(RpcError, r"exited \(code 7\)"):
                server.request("exit", timeout=1)
            self.assertFalse(server.alive)
        finally:
            server.close()

    def test_timeout_removes_pending_request_and_connection_remains_usable(self):
        with fake_server() as server:
            with self.assertRaisesRegex(RpcError, "timed out"):
                server.request("never", timeout=0.03)
            self.assertEqual(server._pending, {})
            self.assertEqual(server.request("model/list")["models"][0]["id"], "fake-model")

    def test_malformed_json_fails_pending_calls_and_channel(self):
        with fake_server() as server:
            with self.assertRaisesRegex(RpcError, "malformed app-server JSONL"):
                server.request("malformed", timeout=1)
            wait_until_dead(server)
            self.assertFalse(server.alive)

    def test_overlong_jsonl_frame_fails_channel(self):
        with fake_server() as server:
            with self.assertRaisesRegex(RpcError, "exceeds"):
                server.request("overlong", {"limit": 4 * 1024 * 1024}, timeout=2)
            wait_until_dead(server)
            self.assertFalse(server.alive)

    def test_stderr_tail_is_bounded_and_available_for_diagnostics(self):
        with fake_server() as server:
            self.assertEqual(server.request("stderr"), {"written": True})
            tail = server.stderr_tail
            self.assertLessEqual(len(tail.encode("utf-8")), 64 * 1024)
            self.assertTrue(tail.endswith("x"))

    def test_close_unblocks_a_waiting_request(self):
        server = fake_server()
        server.start()
        outcome: list[BaseException] = []
        event_outcome: list[dict | None] = []

        def wait_forever() -> None:
            try:
                server.request("never", timeout=10)
            except BaseException as exc:
                outcome.append(exc)

        def wait_for_event() -> None:
            event_outcome.append(server.next_event(timeout=None))

        worker = threading.Thread(target=wait_forever)
        event_worker = threading.Thread(target=wait_for_event)
        worker.start()
        event_worker.start()
        deadline = time.monotonic() + 1
        while not server._pending and time.monotonic() < deadline:
            time.sleep(0.005)
        server.close()
        worker.join(timeout=1)
        event_worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertFalse(event_worker.is_alive())
        self.assertFalse(server._reader_thread.is_alive())
        self.assertFalse(server._stderr_thread.is_alive())
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], RpcError)
        self.assertEqual(event_outcome, [None])


if __name__ == "__main__":
    unittest.main()
