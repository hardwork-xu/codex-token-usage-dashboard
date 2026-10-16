"""Isolated worker lifecycle tests without launchctl, Codex, or account reads."""
from __future__ import annotations

import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import meter
from service_lifecycle import activation_marker


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


class ActivationLifecycleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="meter-lifecycle-fixture-")
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.url = "http://127.0.0.1:50984/"
        meter.write_json(self.folder / "endpoint.json", {"url": self.url, "pid": 100})

    def activation_context(self, clock=None):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(meter, "read_service_mode", return_value=activation_marker()))
        stack.enter_context(mock.patch.object(meter, "locked", return_value=contextlib.nullcontext()))
        if clock is not None:
            stack.enter_context(mock.patch.object(meter.time, "monotonic", side_effect=clock.monotonic))
            stack.enter_context(mock.patch.object(meter.time, "sleep", side_effect=clock.sleep))
        return stack

    def test_socket_mode_timeout_never_starts_competing_process(self):
        clock = FakeClock()
        original = meter.read_json(self.folder / "endpoint.json", {})
        with self.activation_context(clock), \
                mock.patch.object(meter, "local_get", side_effect=OSError("synthetic activation timeout")), \
                mock.patch.object(meter.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(RuntimeError, "按需入口未能唤醒"):
                meter.ensure_service(self.folder)
            spawn.assert_not_called()
        self.assertGreaterEqual(clock.now, 12)
        self.assertLess(clock.now, 13)
        self.assertEqual(meter.read_json(self.folder / "endpoint.json", {}), original)

    def test_socket_mode_waits_for_delayed_activation_past_manual_deadline(self):
        clock = FakeClock()
        def get(url):
            self.assertEqual(url, self.url + "health")
            if clock.now < 8:
                # Model a bounded cold-start HTTP timeout without real sleeping.
                clock.now += 2
                raise OSError("synthetic cold start")
            return {"app": "codex-usage-meter", "pid": 200}
        with self.activation_context(clock), mock.patch.object(meter, "local_get", side_effect=get), \
                mock.patch.object(meter.subprocess, "Popen") as spawn:
            self.assertEqual(meter.ensure_service(self.folder), self.url)
            spawn.assert_not_called()
        self.assertGreater(clock.now, 5)
        self.assertLess(clock.now, 12)

    def test_socket_mode_can_wake_from_marker_before_endpoint_exists(self):
        (self.folder / "endpoint.json").unlink()
        with self.activation_context(), \
                mock.patch.object(meter, "local_get", return_value={"app": "codex-usage-meter", "pid": 200}) as get, \
                mock.patch.object(meter.subprocess, "Popen") as spawn:
            self.assertEqual(meter.ensure_service(self.folder), self.url)
            get.assert_called_once_with(self.url + "health")
            spawn.assert_not_called()

    def test_saved_url_mismatch_fails_before_connecting_or_spawning(self):
        saved = {"url": "http://127.0.0.1:50985/", "pid": 100}
        meter.write_json(self.folder / "endpoint.json", saved)
        with self.activation_context(), mock.patch.object(meter, "local_get") as get, \
                mock.patch.object(meter.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(RuntimeError, "地址不一致"):
                meter.ensure_service(self.folder)
            get.assert_not_called()
            spawn.assert_not_called()
        self.assertEqual(meter.read_json(self.folder / "endpoint.json", {}), saved)

    def test_invalid_marker_never_falls_back_to_manual_start(self):
        with mock.patch.object(meter, "read_service_mode", side_effect=RuntimeError("invalid fixture marker")), \
                mock.patch.object(meter, "local_get") as get, \
                mock.patch.object(meter.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(RuntimeError, "invalid fixture marker"):
                meter.ensure_service(self.folder)
            get.assert_not_called()
            spawn.assert_not_called()

    def test_manual_mode_retains_short_deadline_and_single_background_start(self):
        clock = FakeClock()
        with mock.patch.object(meter, "read_service_mode", return_value=None), \
                mock.patch.object(meter, "locked", return_value=contextlib.nullcontext()), \
                mock.patch.object(meter.time, "monotonic", side_effect=clock.monotonic), \
                mock.patch.object(meter.time, "sleep", side_effect=clock.sleep), \
                mock.patch.object(meter, "local_get", side_effect=OSError("synthetic startup failure")), \
                mock.patch.object(meter.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(RuntimeError, "面板未能启动"):
                meter.ensure_service(self.folder)
            spawn.assert_called_once()
            self.assertIn("serve", spawn.call_args.args[0])
        self.assertGreaterEqual(clock.now, 5)
        self.assertLess(clock.now, 6)

    def test_stop_rereads_new_pid_after_health_wakes_a_worker(self):
        def get(url):
            if url == self.url + "health":
                meter.write_json(self.folder / "endpoint.json", {"url": self.url, "pid": 200})
                return {"app": "codex-usage-meter", "pid": 200}
            self.assertEqual(url, self.url + "api/state")
            return {"csrfToken": "synthetic-token"}
        with mock.patch.object(meter, "local_get", side_effect=get) as get_mock, \
                mock.patch.object(meter, "local_shutdown") as shutdown:
            self.assertTrue(meter.stop_service(self.folder))
            self.assertEqual(get_mock.call_count, 2)
            shutdown.assert_called_once_with(self.url, "synthetic-token")
        self.assertEqual(meter.read_json(self.folder / "endpoint.json", {}), {"url": self.url, "pid": 200})

    def test_stop_does_not_follow_endpoint_address_replacement(self):
        def get(url):
            self.assertEqual(url, self.url + "health")
            meter.write_json(self.folder / "endpoint.json", {"url": "http://127.0.0.1:50985/", "pid": 200})
            return {"app": "codex-usage-meter", "pid": 200}
        with mock.patch.object(meter, "local_get", side_effect=get) as get_mock, \
                mock.patch.object(meter, "local_shutdown") as shutdown:
            self.assertFalse(meter.stop_service(self.folder))
            get_mock.assert_called_once()
            shutdown.assert_not_called()

    def test_invalid_serve_parameters_fail_before_state_or_socket_operations(self):
        invalid = [
            {"port": True}, {"port": -1}, {"port": 65536}, {"port": "50984"}, {"port": 1.5},
            {"idle_timeout": True}, {"idle_timeout": -1}, {"idle_timeout": 86401},
            {"idle_timeout": "600"}, {"launchd_socket": "OtherSocket"},
        ]
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs), mock.patch.object(meter, "locked") as lock, \
                    mock.patch.object(meter, "Meter") as state, \
                    mock.patch.object(meter, "MeterHTTPServer") as server, \
                    mock.patch.object(meter, "activate_launchd_socket") as activate:
                with self.assertRaises(ValueError):
                    meter.serve(self.folder, **kwargs)
                lock.assert_not_called()
                state.assert_not_called()
                server.assert_not_called()
                activate.assert_not_called()

    def test_socket_mode_requires_fixed_port_before_creating_state(self):
        (self.folder / "endpoint.json").unlink()
        with mock.patch.object(meter, "Meter") as state, \
                mock.patch.object(meter, "activate_launchd_socket") as activate:
            with self.assertRaisesRegex(ValueError, "有效的固定端口"):
                meter.serve(self.folder, launchd_socket="MeterHTTP")
            state.assert_not_called()
            activate.assert_not_called()
        self.assertFalse((self.folder / "endpoint.json").exists())

    def test_explicit_launchd_port_mismatch_does_not_activate_or_rewrite_endpoint(self):
        endpoint_path = self.folder / "endpoint.json"
        original = endpoint_path.read_bytes()
        with mock.patch.object(meter, "Meter") as state, \
                mock.patch.object(meter, "activate_launchd_socket") as activate, \
                mock.patch.object(meter, "adopt_http_socket") as adopt, \
                mock.patch.object(meter, "write_json") as write:
            with self.assertRaisesRegex(RuntimeError, "地址不一致，未更换地址"):
                meter.serve(self.folder, port=50985, launchd_socket="MeterHTTP", idle_timeout=2)
            state.assert_not_called()
            activate.assert_not_called()
            adopt.assert_not_called()
            write.assert_not_called()
        self.assertEqual(endpoint_path.read_bytes(), original)

    def test_serve_adopts_saved_listener_drains_requests_and_preserves_url(self):
        listener = object()
        state = mock.Mock()
        server = SimpleNamespace(server_port=50984, serve_forever=mock.Mock(), server_close=mock.Mock())
        with mock.patch.object(meter, "Meter", return_value=state), \
                mock.patch.object(meter, "activate_launchd_socket", return_value=listener) as activate, \
                mock.patch.object(meter, "adopt_http_socket", return_value=server) as adopt, \
                mock.patch.object(meter.threading, "Thread") as background, \
                mock.patch("builtins.print"):
            meter.serve(self.folder, launchd_socket="MeterHTTP", idle_timeout=600)
        activate.assert_called_once_with("MeterHTTP", 50984)
        adopt.assert_called_once_with(meter.MeterHTTPServer, meter.Handler, listener, 50984)
        self.assertIs(server.meter, state)
        self.assertFalse(server.daemon_threads)
        self.assertEqual(server.idle_timeout, 600)
        server.serve_forever.assert_called_once()
        server.server_close.assert_called_once()
        self.assertTrue(server.stop_event.is_set())
        self.assertEqual(background.return_value.start.call_count, 2)
        endpoint = meter.read_json(self.folder / "endpoint.json", {})
        self.assertEqual(endpoint["url"], self.url)
        self.assertEqual(endpoint["pid"], meter.os.getpid())


class IdleLifecycleTests(unittest.TestCase):
    @staticmethod
    def fake_server():
        server = meter.MeterHTTPServer.__new__(meter.MeterHTTPServer)
        server.activity_lock = threading.Lock()
        server.active_requests = 0
        server.last_request_at = time.monotonic() - 100
        server.idle_timeout = 10
        server.idle_shutdown_started = False
        server.stop_event = threading.Event()
        server.shutdown = mock.Mock()
        return server

    def test_idle_shutdown_is_once_and_manual_mode_does_not_idle_out(self):
        server = self.fake_server()
        with mock.patch.object(meter.threading, "Thread") as worker:
            server.idle_timeout = 0
            server.service_actions()
            self.assertFalse(server.stop_event.is_set())
            worker.assert_not_called()
            server.idle_timeout = 10
            server.service_actions()
            server.service_actions()
            self.assertTrue(server.stop_event.is_set())
            worker.assert_called_once_with(target=server.shutdown, daemon=True)
            worker.return_value.start.assert_called_once()

    def test_failed_request_thread_creation_restores_active_count(self):
        server = self.fake_server()
        with mock.patch.object(ThreadingHTTPServer, "process_request", side_effect=RuntimeError("synthetic failure")):
            with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                server.process_request(mock.Mock(), ("127.0.0.1", 12345))
        self.assertEqual(server.active_requests, 0)

    def test_live_accepted_request_blocks_idle_exit_and_completion_resets_clock(self):
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                entered.set()
                if not release.wait(3):
                    return
                content = b"synthetic request finished"
                self.send_response(200)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            def log_message(self, *args):
                pass
        class Server(meter.MeterHTTPServer):
            def process_request_thread(self, *args):
                try:
                    super().process_request_thread(*args)
                finally:
                    completed.set()

        server = Server(("127.0.0.1", 0), Handler)
        server.stop_event = threading.Event()
        server.idle_timeout = 3600
        server.daemon_threads = False
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        worker.start()
        try:
            with socket.create_connection(server.server_address, timeout=2) as client:
                client.sendall(b"GET / HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
                self.assertTrue(entered.wait(2), "synthetic request should enter handler")
                with server.activity_lock:
                    self.assertEqual(server.active_requests, 1)
                    server.last_request_at = time.monotonic() - 7200
                server.service_actions()
                self.assertFalse(server.stop_event.is_set())
                self.assertFalse(server.idle_shutdown_started)
                self.assertTrue(worker.is_alive())
                release.set()
                result = b""
                while True:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    result += chunk
                self.assertTrue(result.endswith(b"synthetic request finished"))
            self.assertTrue(completed.wait(2))
            with server.activity_lock:
                self.assertEqual(server.active_requests, 0)
                self.assertLess(time.monotonic() - server.last_request_at, 2)
            server.service_actions()
            self.assertFalse(server.stop_event.is_set())
            with server.activity_lock:
                server.last_request_at = time.monotonic() - 7200
            server.service_actions()
            self.assertTrue(server.stop_event.wait(2))
            worker.join(2)
            self.assertFalse(worker.is_alive())
        finally:
            release.set()
            server.shutdown()
            worker.join(3)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
