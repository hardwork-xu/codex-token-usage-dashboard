"""A slow local summary must not inherit the short health-check deadline."""
from __future__ import annotations

import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import meter


class SummaryTimeoutTests(unittest.TestCase):
    def test_mcp_summary_accepts_slow_loopback_response_and_preserves_status(self):
        payload = {
            "csrfToken": "synthetic-secret-not-for-model",
            "turns": [{"id": "synthetic-turn-" + str(index)} for index in range(15)],
            "monitoring": {"status": "partial", "collection": {
                "pendingTasks": 2, "unreadableTasks": 3, "writeErrors": 0}},
            "periods": {"today": {"status": "partial", "partial": True,
                "unpricedTokens": 17, "usd": "0.123456"}},
        }
        observed_paths = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                observed_paths.append(self.path)
                # Exercise the actual socket timeout, rather than mock a slow
                # JSON loader. No account, Codex or model is contacted.
                time.sleep(2.2)
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = "http://127.0.0.1:" + str(server.server_port) + "/"
        message = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                   "params": {"name": "get_usage_summary", "arguments": {}}}
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(meter, "ensure_service", return_value=url), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(message) + "\n")), \
                contextlib.redirect_stdout(output):
            meter.mcp(Path(folder))
        response = json.loads(output.getvalue())
        self.assertEqual(response["id"], 7)
        self.assertNotIn("error", response)
        summary = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(observed_paths, ["/api/state"])
        self.assertNotIn("csrfToken", summary)
        self.assertNotIn(payload["csrfToken"], output.getvalue())
        self.assertEqual(summary["turns"], payload["turns"][:10])
        self.assertEqual(summary["monitoring"], payload["monitoring"])
        self.assertEqual(summary["periods"], payload["periods"])

    def test_only_state_reads_receive_the_longer_timeout(self):
        for path, timeout in (("/", 2), ("/health", 2), ("/api/state", 30)):
            with self.subTest(path=path), mock.patch.object(meter.request, "build_opener") as build:
                build.return_value.open.return_value = io.StringIO("{}")
                url = "http://127.0.0.1:51234" + path
                self.assertEqual(meter.local_get(url), {})
                build.return_value.open.assert_called_once_with(url, timeout=timeout)

    def test_longer_summary_timeout_does_not_expand_allowed_addresses(self):
        rejected = (
            "https://127.0.0.1:51234/api/state",
            "http://localhost:51234/api/state",
            "http://example.invalid:51234/api/state",
            "http://user@127.0.0.1:51234/api/state",
            "http://127.0.0.1:51234/api/state?extra=1",
            "http://127.0.0.1:51234/api/state#fragment",
            "http://127.0.0.1:51234/api/other",
        )
        with mock.patch.object(meter.request, "build_opener") as build:
            for url in rejected:
                with self.subTest(url=url), self.assertRaisesRegex(RuntimeError, "地址无效"):
                    meter.local_get(url)
            build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
