"""Marketplace installation entry tests; synthetic logs and temporary folders only."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import run as entry
import meter
import browser_access
import install


def wait_for_owned_worker_exit(pid, timeout=10):
    """Wait for a verified fixture worker, including its detached Windows process."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:  # ERROR_INVALID_PARAMETER: this PID already exited
                return
            raise ctypes.WinError(error)
        try:
            result = kernel.WaitForSingleObject(handle, int(timeout * 1000))
            if result != 0:
                raise AssertionError(f"fixture worker {pid} did not exit: wait result {result}")
        finally:
            kernel.CloseHandle(handle)
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise AssertionError(f"fixture worker {pid} did not exit")


class MarketplaceEntryTests(unittest.TestCase):
    def test_hook_and_skill_share_data_even_with_host_plugin_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            default = Path(temporary) / "stable-data"
            with mock.patch.dict(os.environ, {"PLUGIN_DATA": str(Path(temporary) / "host-data")}), \
                 mock.patch.object(entry, "default_data_dir", return_value=default):
                with mock.patch.dict(os.environ, {"METER_DATA_DIR": ""}):
                    self.assertEqual(entry.runtime_folder(), default)
                    observed = []
                    with mock.patch.object(meter, "main", side_effect=lambda: observed.append(sys.argv[:])):
                        for command in ("open", "summary", "hook", "stop"):
                            entry.main([command])
                    for arguments in observed:
                        self.assertEqual(arguments[1:3], ["--data-dir", str(default)])

    def test_explicit_data_and_browser_commands_use_identical_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = (Path(temporary) / "数据 Space").resolve()
            with mock.patch.object(browser_access, "install", return_value={}) as invoke, \
                 mock.patch("builtins.print"):
                entry.main(["--data-dir", str(folder), "browser-install"])
            invoke.assert_called_once_with(folder, proxy_http=None)

    def test_published_hook_runs_from_unrelated_directory_and_ignores_plugin_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            plugin = base / "Cache 插件 Space" / "codex-usage-meter"
            shutil.copytree(ROOT / "scripts", plugin / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
            # Keep the real Hook, async wake, service and shutdown lifecycle.
            # Only provider discovery is disabled inside this disposable copy,
            # so even a slow CI worker or a state request cannot read an account.
            with (plugin / "scripts/platform_support.py").open("a", encoding="utf-8") as stream:
                stream.write('\n\ndef codex_command():\n    raise RuntimeError("synthetic marketplace fixture: account access disabled")\n')
            log = base / "synthetic.jsonl"
            log.write_text(json.dumps({"type": "session_meta", "payload": {"id": "synthetic-market-task"}}) + "\n", encoding="utf-8")
            folder, wrong = base / "meter-data", base / "host-data"
            hooks = json.loads((ROOT / "hooks/hooks.json").read_text(encoding="utf-8"))["hooks"]
            handler = hooks["SessionStart"][0]["hooks"][0]
            env = dict(os.environ, PLUGIN_ROOT=str(plugin), PLUGIN_DATA=str(wrong),
                       METER_DATA_DIR=str(folder), PYTHONUTF8="1")
            if os.name == "nt":
                shell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
                self.assertIsNotNone(shell)
                command = [shell, "-NoProfile", "-NonInteractive", "-Command", handler["commandWindows"]]
            else:
                command = ["/bin/sh", "-c", handler["command"]]
            try:
                result = subprocess.run(command, input=json.dumps({"hook_event_name": "SessionStart", "session_id": "synthetic-market-task", "transcript_path": str(log)}).encode("utf-8"),
                                        env=env, cwd=base, capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
                self.assertEqual(result.stdout, b"")
                registry = json.loads((folder / "registry.json").read_text(encoding="utf-8"))
                self.assertEqual(set(registry), {"synthetic-market-task"})
                self.assertFalse(wrong.exists())
            finally:
                # A Hook exits before its wake child. Deleting the fixture now
                # races launch.lock on Windows and leaks a live worker elsewhere.
                deadline = time.monotonic() + 20
                wake = {}
                while time.monotonic() < deadline:
                    wake = meter.read_json(folder / "collection-wake-status.json", {})
                    if wake:
                        break
                    time.sleep(0.05)
                endpoint = meter.read_json(folder / "endpoint.json", {})
                if endpoint:
                    url = endpoint.get("url")
                    self.assertTrue(meter.local_url(url))
                    health = meter.local_get(url + "health")
                    self.assertEqual(health.get("app"), "codex-usage-meter")
                    self.assertEqual(health.get("pid"), endpoint.get("pid"))
                    self.assertTrue(meter.stop_service(folder))
                    wait_for_owned_worker_exit(health["pid"])
                # Acquiring both normal locks verifies that the asynchronous
                # launcher and service released their actual file handles.
                for name in ("launch", "service"):
                    with meter.locked(folder, name):
                        pass
                self.assertEqual(wake.get("status"), "ready", wake)

    def test_source_copy_excludes_checkout_data_and_marketplace(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, target = Path(temporary) / "source", Path(temporary) / "target"
            (source / "scripts").mkdir(parents=True)
            (source / "scripts/run.py").write_text("# synthetic runtime\n")
            for name in ("dev-data/settings.json", ".agents/plugins/marketplace.json", "private-notes.txt"):
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("synthetic private fixture")
            install.copy_plugin_source(source, target)
            self.assertEqual([p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()], ["scripts/run.py"])


if __name__ == "__main__":
    unittest.main()
