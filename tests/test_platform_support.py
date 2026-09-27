"""Platform adapters and safe lifecycle checks, with no real account calls."""
from __future__ import annotations

from email.message import Message
import errno
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import meter
import platform_support as platform


class PlatformTests(unittest.TestCase):
    def test_path_cli_precedes_macos_app_fallback_without_home_lookup(self):
        with mock.patch.object(platform, "is_windows", return_value=False), \
                mock.patch.object(platform.sys, "platform", "darwin"), \
                mock.patch.object(platform.shutil, "which", return_value="/synthetic/bin/codex"), \
                mock.patch.object(Path, "home", side_effect=AssertionError("PATH takes precedence")):
            self.assertEqual(platform.codex_command(), ["/synthetic/bin/codex"])

    def test_macos_app_layouts_work_without_shell_path_or_recursive_discovery(self):
        home = Path("/synthetic/home")
        for applications in (Path("/Applications"), home / "Applications"):
            for app in ("ChatGPT.app", "Codex.app"):
                for layout in ("Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex",
                               "Contents/Resources/codex"):
                    expected = applications / app / layout
                    with self.subTest(app=app, layout=layout, applications=applications), \
                            mock.patch.object(platform, "is_windows", return_value=False), \
                            mock.patch.object(platform.sys, "platform", "darwin"), \
                            mock.patch.object(platform.shutil, "which", return_value=None), \
                            mock.patch.object(Path, "home", return_value=home), \
                            mock.patch.object(Path, "is_file", autospec=True, side_effect=lambda path: path == expected), \
                            mock.patch.object(platform.os, "access", return_value=True) as access, \
                            mock.patch.object(Path, "rglob", side_effect=AssertionError("no app scan")), \
                            mock.patch.object(platform.subprocess, "Popen") as popen:
                        self.assertEqual(platform.codex_command(), [str(expected)])
                        access.assert_called_once_with(expected, os.X_OK)
                        popen.assert_not_called()

    def test_macos_skips_inaccessible_or_nonexecutable_candidates(self):
        candidates = [Path("/synthetic/denied"), Path("/synthetic/not-executable"), Path("/synthetic/working")]
        with mock.patch.object(platform, "is_windows", return_value=False), \
                mock.patch.object(platform.sys, "platform", "darwin"), \
                mock.patch.object(platform.shutil, "which", return_value=None), \
                mock.patch.object(platform, "_macos_cli_candidates", return_value=iter(candidates)), \
                mock.patch.object(Path, "is_file", side_effect=[PermissionError("synthetic denied"), True, True]), \
                mock.patch.object(platform.os, "access", side_effect=[False, True]):
            self.assertEqual(platform.codex_command(), [str(candidates[-1])])

    def test_missing_cli_does_not_diagnose_login_or_expose_paths(self):
        for system in ("darwin", "linux"):
            with self.subTest(system=system), \
                    mock.patch.object(platform, "is_windows", return_value=False), \
                    mock.patch.object(platform.sys, "platform", system), \
                    mock.patch.object(platform.shutil, "which", return_value=None), \
                    mock.patch.object(platform, "_macos_cli_candidates", return_value=iter([Path("/synthetic/private-path")])), \
                    mock.patch.object(Path, "is_file", return_value=False), \
                    self.assertRaisesRegex(RuntimeError, "找不到可运行的 Codex CLI") as caught:
                platform.codex_command()
            self.assertNotIn("登录", str(caught.exception))
            self.assertNotIn("private-path", str(caught.exception))

    def test_default_paths_and_explicit_data_override(self):
        with mock.patch.object(platform, "is_windows", return_value=True), mock.patch.dict(os.environ, {"LOCALAPPDATA": "/synthetic/local"}):
            self.assertEqual(platform.default_data_dir(), Path("/synthetic/local/Codex Usage Meter"))
        with mock.patch.object(platform, "is_windows", return_value=True), mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(Path, "home", return_value=Path("/synthetic/home")):
            self.assertEqual(platform.default_data_dir(), Path("/synthetic/home/AppData/Local/Codex Usage Meter"))
        with mock.patch.object(platform, "is_windows", return_value=False), mock.patch.object(Path, "home", return_value=Path("/synthetic/home")):
            self.assertEqual(platform.default_data_dir(), Path("/synthetic/home/Library/Application Support/Codex Usage Meter"))
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(meter, "default_data_dir", side_effect=AssertionError("must use explicit path")):
            self.assertEqual(meter.data_dir(temp), Path(temp).resolve())

    def test_windows_native_cli_precedes_npm_and_documented_install_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            native = Path(temp) / "codex.exe"
            native.touch()
            with mock.patch.object(platform, "is_windows", return_value=True), mock.patch.object(platform.shutil, "which", side_effect=lambda name: str(native) if name == "codex.exe" else None):
                self.assertEqual(platform.codex_command(), [str(native)])
            standalone = Path(temp) / "Programs/OpenAI/Codex/bin/codex.exe"
            standalone.parent.mkdir(parents=True)
            standalone.touch()
            with mock.patch.object(platform, "is_windows", return_value=True), mock.patch.dict(os.environ, {"LOCALAPPDATA": temp}), mock.patch.object(platform.shutil, "which", return_value=None):
                self.assertEqual(platform.codex_command(), [str(standalone)])

    def test_npm_shim_is_resolved_to_node_without_executing_shell(self):
        with tempfile.TemporaryDirectory(prefix="meter with spaces ") as temp:
            folder = Path(temp)
            shim = folder / "codex.cmd"
            shim.write_text('@"%dp0%\\node.exe" "%dp0%\\node_modules\\@openai\\codex\\bin\\codex.js" %*\n', encoding="utf-8")
            node = folder / "node.exe"
            node.touch()
            script = folder / "node_modules/@openai/codex/bin/codex.js"
            script.parent.mkdir(parents=True)
            script.touch()
            with mock.patch.object(platform, "is_windows", return_value=True), mock.patch.dict(os.environ, {"LOCALAPPDATA": temp}), mock.patch.object(platform.shutil, "which", side_effect=lambda name: str(shim) if name in ("codex", "codex.cmd") else None), mock.patch.object(subprocess, "Popen") as popen:
                self.assertEqual(platform.codex_command(), [str(node), str(script)])
                popen.assert_not_called()
                shim.write_text('@powershell arbitrary-command\n', encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "官方 Windows CLI"):
                    platform.codex_command()

    def test_unavailable_cli_and_missing_node_fail_clearly(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(platform, "is_windows", return_value=True), mock.patch.dict(os.environ, {"LOCALAPPDATA": temp}), mock.patch.object(platform.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "找不到"):
                platform.codex_command()
            shim = Path(temp) / "codex.cmd"
            shim.write_text('"%dp0%\\node_modules\\@openai\\codex\\bin\\codex.js"', encoding="utf-8")
            script = Path(temp) / "node_modules/@openai/codex/bin/codex.js"
            script.parent.mkdir(parents=True)
            script.touch()
            self.assertIsNone(platform._npm_command(shim))

    def test_pathext_result_cannot_turn_native_lookup_into_batch_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            batch = Path(temp) / "codex.exe.cmd"
            batch.write_text("@echo arbitrary batch\n", encoding="utf-8")
            with mock.patch.object(platform, "is_windows", return_value=True), mock.patch.dict(os.environ, {"LOCALAPPDATA": temp}), mock.patch.object(platform.shutil, "which", side_effect=lambda name: str(batch) if name == "codex.exe" else None):
                with self.assertRaises(RuntimeError):
                    platform.codex_command()

    def test_startup_flags_do_not_invoke_a_shell_or_mix_detachment_modes(self):
        with mock.patch.object(platform, "is_windows", return_value=True):
            foreground = platform.subprocess_options()
            background = platform.subprocess_options(background=True)
        self.assertEqual(foreground, {"close_fds": True, "creationflags": 0x08000000})
        self.assertEqual(background, {"close_fds": True, "creationflags": 0x00000208})
        with mock.patch.object(platform, "is_windows", return_value=False):
            self.assertEqual(platform.subprocess_options(background=True), {"close_fds": True, "start_new_session": True})
            self.assertEqual(platform.subprocess_options(), {"close_fds": True})


class LockTests(unittest.TestCase):
    def test_cross_process_contention_times_out_then_acquires_after_release(self):
        child = ("import sys\nfrom platform_support import file_lock\n"
                 "try:\n with file_lock(sys.argv[1], timeout=0.15): print('acquired')\n"
                 "except TimeoutError: print('blocked')\n")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "shared.lock"
            env = {**os.environ, "PYTHONPATH": str(SCRIPTS)}
            def run():
                return subprocess.run([sys.executable, "-c", child, str(path)], env=env, capture_output=True,
                                      text=True, encoding="utf-8", timeout=5, check=True, **platform.subprocess_options()).stdout.strip()
            with platform.file_lock(path):
                self.assertEqual(run(), "blocked")
            self.assertEqual(run(), "acquired")

    def test_windows_byte_zero_is_locked_before_any_write_and_released_on_exception(self):
        calls = []
        def locking(fd, mode, size):
            calls.append((fd, mode, size, os.lseek(fd, 0, os.SEEK_CUR), os.fstat(fd).st_size))
        fake = SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=locking)
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(platform, "is_windows", return_value=True), mock.patch.dict(sys.modules, {"msvcrt": fake}):
            path = Path(temp) / "empty.lock"
            with self.assertRaisesRegex(ValueError, "body failed"):
                with platform.file_lock(path):
                    raise ValueError("body failed")
            self.assertEqual([(row[1], row[2], row[3], row[4]) for row in calls], [(2, 1, 0, 0), (0, 1, 0, 0)])
            with self.assertRaises(OSError):
                os.fstat(calls[0][0])

    def test_windows_lock_timeout_never_unlocks_an_unacquired_region(self):
        fake = SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=mock.Mock(side_effect=OSError(errno.EACCES, "busy")))
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(platform, "is_windows", return_value=True), mock.patch.dict(sys.modules, {"msvcrt": fake}):
            with self.assertRaises(TimeoutError):
                with platform.file_lock(Path(temp) / "busy.lock", timeout=0):
                    self.fail("must not enter")
        self.assertEqual(fake.locking.call_count, 1)
        self.assertEqual(fake.locking.call_args.args[1:], (2, 1))

    def test_unexpected_lock_errors_are_not_misreported_as_contention(self):
        fake = SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=mock.Mock(side_effect=OSError(errno.EBADF, "bad descriptor")))
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(platform, "is_windows", return_value=True), mock.patch.dict(sys.modules, {"msvcrt": fake}):
            with self.assertRaises(OSError) as raised:
                with platform.file_lock(Path(temp) / "error.lock"):
                    self.fail("must not enter")
            self.assertEqual(raised.exception.errno, errno.EBADF)


class FileWriteTests(unittest.TestCase):
    def test_windows_replacement_retries_only_the_same_serialized_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "registry.json"
            original_replace = os.replace
            attempts = []
            def replace(source, target):
                attempts.append((source, target))
                if len(attempts) == 1:
                    raise PermissionError("reader open")
                original_replace(source, target)
            with mock.patch.object(meter, "is_windows", return_value=True), mock.patch.object(meter.os, "replace", side_effect=replace), mock.patch.object(meter.time, "sleep"):
                meter.write_json(path, {"name": "中文"})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"name": "中文"})
            self.assertEqual(attempts[0], attempts[1])
            self.assertEqual(list(Path(temp).glob("*.tmp")), [])

    def test_failed_replacement_preserves_old_data_and_removes_temp(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.json"
            path.write_text('{"old":true}', encoding="utf-8")
            with mock.patch.object(meter, "is_windows", return_value=True), mock.patch.object(meter.os, "replace", side_effect=PermissionError("denied")) as replace, mock.patch.object(meter.time, "sleep"):
                with self.assertRaises(PermissionError):
                    meter.write_json(path, {"new": True})
            self.assertEqual(replace.call_count, 11)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"old": True})
            self.assertEqual(list(Path(temp).glob("*.tmp")), [])


class ServerBindingTests(unittest.TestCase):
    def test_numeric_loopback_start_does_not_wait_for_dns(self):
        with mock.patch.object(meter.socket, "getfqdn", side_effect=AssertionError("No DNS needed")), \
                mock.patch.object(meter.socket, "gethostbyaddr", side_effect=AssertionError("No reverse DNS needed")):
            with meter.MeterHTTPServer(("127.0.0.1", 0), meter.Handler) as server:
                self.assertEqual(server.server_name, "127.0.0.1")
                self.assertGreater(server.server_port, 0)
                with meter.socket.create_connection((server.server_name, server.server_port), timeout=1):
                    pass


class ShutdownTests(unittest.TestCase):
    def handler(self, headers):
        result = meter.Handler.__new__(meter.Handler)
        result.headers = Message()
        for key, value in headers.items():
            result.headers[key] = value
        result.path = "/api/shutdown"
        result.server = SimpleNamespace(server_port=9876, meter=SimpleNamespace(csrf="valid-token"),
                                        stop_event=mock.Mock(), shutdown=mock.Mock())
        result.respond = mock.Mock()
        return result

    def test_shutdown_preserves_host_origin_and_token_checks(self):
        for changed in ({"X-Meter-Token": "wrong"}, {"Host": "evil.test"}, {"Origin": "https://evil.test"}, {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(changed=changed), mock.patch.object(meter.threading, "Thread") as thread:
                handler = self.handler({"Host": "127.0.0.1:9876", "X-Meter-Token": "valid-token", **changed})
                handler.do_POST()
                self.assertEqual(handler.respond.call_args.args[1], 403)
                handler.server.stop_event.set.assert_not_called()
                thread.assert_not_called()
        with mock.patch.object(meter.threading, "Thread") as thread:
            handler = self.handler({"Host": "127.0.0.1:9876", "Origin": "http://127.0.0.1:9876", "X-Meter-Token": "valid-token"})
            handler.do_POST()
            handler.respond.assert_called_once_with({"ok": True})
            handler.server.stop_event.set.assert_called_once()
            thread.assert_called_once_with(target=handler.server.shutdown, daemon=True)
            thread.return_value.start.assert_called_once()

    def test_stale_pid_or_different_local_app_never_receives_shutdown(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            meter.write_json(folder / "endpoint.json", {"url": "http://127.0.0.1:9876/", "pid": 100})
            for health in ({"app": "other", "pid": 100}, {"app": "codex-usage-meter", "pid": 101}):
                with self.subTest(health=health), mock.patch.object(meter, "local_get", return_value=health), mock.patch.object(meter, "local_shutdown") as shutdown:
                    self.assertFalse(meter.stop_service(folder))
                    shutdown.assert_not_called()

    def test_stop_authenticates_and_preserves_endpoint_for_stable_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            endpoint = {"url": "http://127.0.0.1:9876/", "pid": 100}
            meter.write_json(folder / "endpoint.json", endpoint)
            with mock.patch.object(meter, "local_get", side_effect=[{"app": "codex-usage-meter", "pid": 100}, {"csrfToken": "token"}]), mock.patch.object(meter, "local_shutdown") as shutdown:
                self.assertTrue(meter.stop_service(folder))
                shutdown.assert_called_once_with(endpoint["url"], "token")
            self.assertEqual(meter.read_json(folder / "endpoint.json", {}), endpoint)

    def test_shutdown_request_rejects_foreign_address_or_invalid_token_before_io(self):
        for url, token in (("http://evil.test:1234/", "ok"), ("http://127.0.0.1:1234/api/state", "ok"),
                           ("http://127.0.0.1:1234/", "坏"), ("http://127.0.0.1:1234/", None)):
            with self.subTest(url=url, token=token), mock.patch.object(meter.request, "build_opener") as opener:
                with self.assertRaises(RuntimeError):
                    meter.local_shutdown(url, token)
                opener.assert_not_called()

    def test_shutdown_post_disables_proxies_redirects_and_supplies_token(self):
        response = io.StringIO('{"ok":true}')
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(meter.request, "build_opener", return_value=opener) as build:
            meter.local_shutdown("http://127.0.0.1:9876/", "valid-token")
        handlers = build.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsInstance(handlers[1], meter.NoRedirect)
        message = opener.open.call_args.args[0]
        self.assertEqual(message.get_method(), "POST")
        self.assertEqual(message.full_url, "http://127.0.0.1:9876/api/shutdown")
        self.assertEqual(message.get_header("X-meter-token"), "valid-token")
        self.assertEqual(message.get_header("Origin"), "http://127.0.0.1:9876")


if __name__ == "__main__":
    unittest.main()
