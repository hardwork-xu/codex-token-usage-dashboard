"""Synthetic browser-access configuration tests; never call real launchctl."""
from contextlib import nullcontext, redirect_stdout
import io
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import browser_access as access
import meter
from service_lifecycle import activation_marker, build_launch_agent, MODE_FILE


class BrowserAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve() / "Synthetic 用户 Home"
        self.folder = self.home / "Data folder"
        self.folder.mkdir(parents=True)
        self.plist = self.home / "Library/LaunchAgents/local.codex-usage-meter.plist"
        self.source = self.home / "Arbitrary 缓存 location" / "plugin-version"
        repository = Path(__file__).resolve().parents[1]
        for name in access.RUNTIME_FILES:
            target = self.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((repository / name).read_bytes())
        self.runtime = self.folder / access.RUNTIME_NAME
        self.script = self.runtime / "scripts/meter.py"
        self.endpoint = self.folder / "endpoint.json"
        self.endpoint.write_text(json.dumps({"url": "http://127.0.0.1:51234/", "pid": 123}), encoding="utf-8")
        self.marker = self.folder / MODE_FILE
        self.loaded = False
        self.calls = []
        self.events = []
        self.fail_bootstrap = False
        patches = [mock.patch.object(access.sys, "platform", "darwin"),
                   mock.patch.object(access.Path, "home", return_value=self.home),
                   mock.patch.object(access.os, "getuid", return_value=501, create=True),
                   mock.patch.object(access.subprocess, "run", side_effect=self.fake_launchctl),
                   mock.patch.object(access, "_wait_for_port", side_effect=lambda port: self.events.append(("wait", port))),
                   mock.patch.object(meter, "stop_service", side_effect=lambda folder: self.events.append(("stop", folder)) or True),
                   mock.patch.object(access, "file_lock", side_effect=lambda path: nullcontext()),
                   mock.patch.object(access, "SOURCE_ROOT", self.source)]
        self.mocks = [patch.start() for patch in patches]
        for patch in reversed(patches):
            self.addCleanup(patch.stop)

    def fake_launchctl(self, arguments, **kwargs):
        self.calls.append(arguments)
        self.assertEqual(arguments[0], "/bin/launchctl")
        self.assertNotIn("shell", kwargs)
        command = arguments[1]
        if command == "print":
            return subprocess.CompletedProcess(arguments, 0 if self.loaded else 113,
                                               "job = {\n\tpid = 456\n\tenvironment = { PRIVATE = secret }\n}\n")
        if command == "bootout":
            self.assertEqual(arguments, ["/bin/launchctl", "bootout", "gui/501/local.codex-usage-meter"])
            self.loaded = False
            self.events.append(("bootout",))
            return subprocess.CompletedProcess(arguments, 0, "")
        self.assertEqual(command, "bootstrap")
        self.assertEqual(arguments, ["/bin/launchctl", "bootstrap", "gui/501", str(self.plist)])
        self.events.append(("bootstrap", self.marker.exists()))
        if self.fail_bootstrap:
            self.fail_bootstrap = False
            return subprocess.CompletedProcess(arguments, 5, "PRIVATE failure detail")
        self.loaded = True
        return subprocess.CompletedProcess(arguments, 0, "")

    def seed_installed(self):
        for name, raw in access._runtime_payload(self.source).items():
            access._write(self.runtime / name, raw)
        self.plist.parent.mkdir(parents=True)
        config = build_launch_agent(sys.executable, self.script, self.folder, port=51234)
        self.plist.write_bytes(plistlib.dumps(config, sort_keys=True))
        self.marker.write_text(json.dumps(activation_marker(51234)) + "\n", encoding="utf-8")
        self.loaded = True

    def test_install_uses_stable_script_saved_port_and_marker_after_bootstrap(self):
        original_endpoint = self.endpoint.read_bytes()
        result = access.install(self.folder)
        self.assertEqual(result, {"supported": True, "loaded": True, "workerPid": None,
                                  "port": 51234, "mode": "launchd-socket"})
        config = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(config["ProgramArguments"][:6], [sys.executable, "-X", "utf8", str(self.script), "--data-dir", str(self.folder)])
        self.assertEqual(config["Sockets"]["MeterHTTP"]["SockNodeName"], "127.0.0.1")
        self.assertNotIn("RunAtLoad", config)
        self.assertNotIn("KeepAlive", config)
        self.assertNotIn("EnvironmentVariables", config)
        self.assertEqual(self.events, [("stop", self.folder), ("wait", 51234), ("bootstrap", False)])
        self.assertEqual(json.loads(self.marker.read_text()), activation_marker(51234))
        self.assertEqual(self.endpoint.read_bytes(), original_endpoint)

    def test_upgrade_accepts_owned_legacy_runtime_without_credit_module(self):
        self.seed_installed()
        old_manifest = self.runtime / ".codex-plugin/plugin.json"
        value = json.loads(old_manifest.read_text(encoding="utf-8"))
        value["version"] = "0.9.1"
        old_manifest.write_text(json.dumps(value), encoding="utf-8")
        (self.runtime / "scripts/official_credits.py").unlink()
        result = access.install(self.folder)
        self.assertEqual(result["port"], 51234)
        self.assertTrue((self.runtime / "scripts/official_credits.py").is_file())

    def test_missing_credit_module_in_new_runtime_is_not_silently_accepted(self):
        self.seed_installed()
        old_manifest = self.runtime / ".codex-plugin/plugin.json"
        value = json.loads(old_manifest.read_text(encoding="utf-8"))
        value["version"] = "0.10.0"
        old_manifest.write_text(json.dumps(value), encoding="utf-8")
        (self.runtime / "scripts/official_credits.py").unlink()
        with self.assertRaisesRegex(RuntimeError, "运行文件不完整"):
            access.install(self.folder)

    def test_descendant_runtime_upgrade_preserves_older_owned_install(self):
        self.seed_installed()
        manifest = self.runtime / ".codex-plugin/plugin.json"
        value = json.loads(manifest.read_text(encoding="utf-8"))
        value["version"] = "0.11.0+local.1"
        manifest.write_text(json.dumps(value), encoding="utf-8")
        (self.runtime / "scripts/descendants.py").unlink()
        self.assertEqual(access.install(self.folder)["port"], 51234)
        self.assertTrue((self.runtime / "scripts/descendants.py").is_file())

    def test_new_runtime_missing_descendant_module_is_rejected(self):
        self.seed_installed()
        manifest = self.runtime / ".codex-plugin/plugin.json"
        value = json.loads(manifest.read_text(encoding="utf-8"))
        value["version"] = "0.12.0"
        manifest.write_text(json.dumps(value), encoding="utf-8")
        (self.runtime / "scripts/descendants.py").unlink()
        with self.assertRaisesRegex(RuntimeError, "运行文件不完整"):
            access.install(self.folder)

    def test_no_saved_endpoint_uses_default_or_explicit_port(self):
        self.endpoint.unlink()
        self.assertEqual(access.install(self.folder, port=51345)["port"], 51345)
        access.uninstall(self.folder)
        self.assertEqual(access.install(self.folder)["port"], access.DEFAULT_PORT)

    def test_local_proxy_is_explicit_and_normalized(self):
        access.install(self.folder, proxy_http="http://127.0.0.1:51235")
        config = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(config["EnvironmentVariables"], {
            "HTTP_PROXY": "http://127.0.0.1:51235/", "HTTPS_PROXY": "http://127.0.0.1:51235/",
            "NO_PROXY": "localhost,127.0.0.1,::1"})

    def test_invalid_proxy_and_changed_port_fail_before_os_mutation(self):
        for url in ("http://localhost:51235/", "https://127.0.0.1:51235/", "http://user:pass@127.0.0.1:51235/",
                    "http://127.0.0.1:51235/path", "http://127.0.0.1:51235/?x=1", "http://127.0.0.1:51235/#fragment"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                access.install(self.folder, proxy_http=url)
        with self.assertRaisesRegex(RuntimeError, "不一致"):
            access.install(self.folder, port=51345)
        self.assertFalse(self.plist.exists())
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.calls, [])
        self.mocks[5].assert_not_called()

    def test_repeated_install_is_idempotent(self):
        access.install(self.folder)
        self.events.clear()
        result = access.install(self.folder)
        self.assertTrue(result["loaded"])
        self.assertEqual(self.events, [])

    def test_repeated_install_without_proxy_option_preserves_explicit_proxy(self):
        access.install(self.folder, proxy_http="http://127.0.0.1:51235/")
        original = self.plist.read_bytes()
        self.events.clear()
        result = access.install(self.folder)
        self.assertTrue(result["loaded"])
        self.assertEqual(self.plist.read_bytes(), original)
        self.assertEqual(plistlib.loads(original)["EnvironmentVariables"]["HTTP_PROXY"],
                         "http://127.0.0.1:51235/")
        self.assertEqual(self.events, [])

    def test_saved_proxy_is_revalidated_before_reinstall(self):
        self.seed_installed()
        config = plistlib.loads(self.plist.read_bytes())
        config["EnvironmentVariables"] = {"HTTP_PROXY": "http://user:pass@127.0.0.1:51235/"}
        self.plist.write_bytes(plistlib.dumps(config))
        original = self.plist.read_bytes()
        with self.assertRaises(ValueError):
            access.install(self.folder)
        self.assertEqual(self.plist.read_bytes(), original)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.events, [])

    def test_failed_initial_bootstrap_restores_absent_files(self):
        self.fail_bootstrap = True
        with self.assertRaisesRegex(RuntimeError, "原配置已恢复"):
            access.install(self.folder)
        self.assertFalse(self.plist.exists())
        self.assertFalse(self.marker.exists())
        self.assertTrue(self.endpoint.exists())
        self.assertFalse(self.runtime.exists())
        self.assertEqual(list(self.folder.glob(".browser-runtime-*")), [])

    def test_failed_replacement_restores_previous_configuration_and_job(self):
        self.seed_installed()
        old_plist, old_marker = self.plist.read_bytes(), self.marker.read_bytes()
        self.fail_bootstrap = True
        with self.assertRaisesRegex(RuntimeError, "原配置已恢复"):
            access.install(self.folder, proxy_http="http://127.0.0.1:51235/")
        self.assertEqual(self.plist.read_bytes(), old_plist)
        self.assertEqual(self.marker.read_bytes(), old_marker)
        self.assertTrue(self.loaded)

    def test_failed_marker_write_removes_new_job_and_restores_files(self):
        write = access._write
        def fail_marker(path, content):
            if path == self.marker and content is not None:
                raise OSError("synthetic write failure")
            return write(path, content)
        with mock.patch.object(access, "_write", side_effect=fail_marker), self.assertRaises(RuntimeError):
            access.install(self.folder)
        self.assertFalse(self.loaded)
        self.assertFalse(self.plist.exists())
        self.assertFalse(self.marker.exists())
        self.assertFalse(self.runtime.exists())

    def test_busy_port_does_not_overwrite_config_or_stop_foreign_program(self):
        self.mocks[4].side_effect = RuntimeError("synthetic busy port")
        self.mocks[5].side_effect = None
        self.mocks[5].return_value = False
        with self.assertRaises(RuntimeError):
            access.install(self.folder)
        self.assertFalse(self.plist.exists())
        self.assertFalse(self.marker.exists())
        self.assertTrue(all(call[1] == "print" for call in self.calls))

    def test_status_outputs_only_allowlisted_fields_without_waking_worker(self):
        self.seed_installed()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(access.main(["--data-dir", str(self.folder), "status"]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(set(result), {"supported", "loaded", "workerPid", "port", "mode"})
        self.assertEqual(result["workerPid"], 456)
        self.assertNotIn("PRIVATE", output.getvalue())
        self.mocks[5].assert_not_called()
        self.assertTrue(all(call[1] == "print" for call in self.calls))

    def test_uninstall_only_removes_plugin_service_and_marker(self):
        self.seed_installed()
        original_endpoint = self.endpoint.read_bytes()
        records = self.folder / "synthetic-records.json"
        records.write_text("{}", encoding="utf-8")
        other = self.plist.parent / "other.example.plist"
        other.write_text("do not touch", encoding="utf-8")
        result = access.uninstall(self.folder)
        self.assertFalse(result["loaded"])
        self.assertEqual(result["mode"], "manual")
        self.assertFalse(self.plist.exists())
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.endpoint.read_bytes(), original_endpoint)
        self.assertTrue(records.exists())
        self.assertEqual(other.read_text(), "do not touch")
        access.uninstall(self.folder)  # Idempotent when already absent.

    def test_non_macos_is_explicitly_unsupported_without_process_calls(self):
        with mock.patch.object(access.sys, "platform", "win32"):
            self.assertEqual(access.status(self.folder)["mode"], "unsupported")
            with self.assertRaisesRegex(RuntimeError, "unsupported"):
                access.install(self.folder)
            with self.assertRaisesRegex(RuntimeError, "unsupported"):
                access.uninstall(self.folder)
        self.assertEqual(self.calls, [])

    def test_uninstall_preserves_malformed_endpoint_without_failing_opt_out(self):
        self.seed_installed()
        self.endpoint.write_bytes(b"synthetic invalid endpoint")
        result = access.uninstall(self.folder)
        self.assertEqual(result["mode"], "manual")
        self.assertIsNone(result["port"])
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.endpoint.read_bytes(), b"synthetic invalid endpoint")

    def test_foreign_plist_and_symlinked_endpoint_are_rejected(self):
        self.plist.parent.mkdir(parents=True)
        self.plist.write_bytes(plistlib.dumps({"Label": "other.example"}))
        with self.assertRaisesRegex(RuntimeError, "不属于本插件"):
            access.uninstall(self.folder)
        self.endpoint.unlink()
        target = self.folder / "synthetic-endpoint-copy.json"
        target.write_text('{"url":"http://127.0.0.1:51234/"}', encoding="utf-8")
        try:
            self.endpoint.symlink_to(target)
        except OSError:
            self.skipTest("Creating symlinks is unavailable on this test host")
        with self.assertRaises(RuntimeError):
            access.endpoint_port(self.folder)
        self.assertEqual(self.calls, [])

    def test_runtime_survives_source_cache_removal_without_copying_private_files(self):
        (self.source / "registry.json").write_text('{"synthetic-private":"do not copy"}', encoding="utf-8")
        (self.source / ".mcp.json").write_text('{"private-machine-path":"do not copy"}', encoding="utf-8")
        (self.source / "hooks").mkdir()
        (self.source / "hooks/hooks.json").write_text("{}", encoding="utf-8")
        expected_web = (self.source / "web/index.html").read_bytes()
        access.install(self.folder)
        shutil.rmtree(self.source)
        self.assertEqual((self.runtime / "web/index.html").read_bytes(), expected_web)
        self.assertFalse((self.runtime / "registry.json").exists())
        self.assertFalse((self.runtime / ".mcp.json").exists())
        self.assertFalse((self.runtime / "hooks").exists())
        self.assertNotIn(str(self.source), self.plist.read_text(encoding="utf-8"))
        # Import the actual copied runtime after the cache disappears. --help
        # never starts a service, queries an account, or reads task records.
        child = subprocess.Popen([sys.executable, "-B", str(self.script), "--help"],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            output, errors = child.communicate(timeout=15)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0, errors.decode("utf-8", errors="replace"))
        self.assertIn(b"--data-dir", output)

    def test_explicit_reinstall_updates_runtime_at_the_same_address(self):
        access.install(self.folder)
        original_plist = self.plist.read_bytes()
        self.events.clear()
        updated = (self.source / "web/index.html").read_bytes() + b"\n<!-- synthetic update -->\n"
        (self.source / "web/index.html").write_bytes(updated)
        access.install(self.folder)
        self.assertEqual((self.runtime / "web/index.html").read_bytes(), updated)
        self.assertEqual(self.plist.read_bytes(), original_plist)
        self.assertEqual(self.events, [("bootout",), ("wait", 51234), ("bootstrap", True)])
        self.assertEqual(list(self.folder.glob(".browser-runtime-*")), [])

    def test_failed_updated_runtime_restores_code_and_previous_job(self):
        access.install(self.folder, proxy_http="http://127.0.0.1:51235/")
        original = access._runtime_payload(self.runtime, managed=True)
        old_plist, old_marker = self.plist.read_bytes(), self.marker.read_bytes()
        (self.source / "scripts/meter.py").write_bytes(b"raise RuntimeError('synthetic replacement')\n")
        self.fail_bootstrap = True
        with self.assertRaisesRegex(RuntimeError, "原配置已恢复"):
            access.install(self.folder)
        self.assertEqual(access._runtime_payload(self.runtime, managed=True), original)
        self.assertEqual(self.plist.read_bytes(), old_plist)
        self.assertEqual(self.marker.read_bytes(), old_marker)
        self.assertTrue(self.loaded)
        self.assertEqual(list(self.folder.glob(".browser-runtime-*")), [])

    def test_failed_directory_swap_restores_previous_runtime_before_restarting(self):
        access.install(self.folder)
        original = access._runtime_payload(self.runtime, managed=True)
        (self.source / "web/index.html").write_bytes(b"synthetic update")
        real_replace = access.os.replace
        def replace(source, destination):
            if Path(source).name.startswith(".browser-runtime-stage-") and Path(destination) == self.runtime:
                raise OSError("synthetic directory replacement failure")
            return real_replace(source, destination)
        with mock.patch.object(access.os, "replace", side_effect=replace), self.assertRaisesRegex(RuntimeError, "原配置已恢复"):
            access.install(self.folder)
        self.assertEqual(access._runtime_payload(self.runtime, managed=True), original)
        self.assertTrue(self.loaded)
        self.assertEqual(list(self.folder.glob(".browser-runtime-*")), [])

    def test_incomplete_source_and_foreign_runtime_are_rejected_before_service_changes(self):
        missing = self.source / "scripts/pricing.py"
        original = missing.read_bytes()
        missing.unlink()
        with self.assertRaisesRegex(RuntimeError, "不完整"):
            access.install(self.folder)
        missing.write_bytes(original)
        self.runtime.mkdir()
        note = self.runtime / "unrelated.txt"
        note.write_text("leave untouched", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "不属于本插件"):
            access.install(self.folder)
        self.assertEqual(note.read_text(), "leave untouched")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.events, [])

    def test_unexpected_managed_runtime_file_is_preserved_and_rejected(self):
        access.install(self.folder)
        self.calls.clear()
        self.events.clear()
        extra = self.runtime / "personal-note.txt"
        extra.write_text("synthetic private note", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "非预期"):
            access.install(self.folder)
        self.assertEqual(extra.read_text(), "synthetic private note")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.events, [])

    def test_source_and_runtime_links_are_rejected_without_following_targets(self):
        script = self.source / "scripts/pricing.py"
        original = script.read_bytes()
        target = self.home / "outside.py"
        target.write_bytes(original)
        script.unlink()
        try:
            script.symlink_to(target)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                self.skipTest("Windows has not granted symlink creation")
            raise
        with self.assertRaises(RuntimeError):
            access.install(self.folder)
        script.unlink()
        script.write_bytes(original)
        outside = self.home / "outside-runtime"
        outside.mkdir()
        self.runtime.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "普通目录"):
            access.install(self.folder)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.events, [])


if __name__ == "__main__":
    unittest.main()
