"""User-invoked macOS browser access management; importing never installs a job."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

from platform_support import default_data_dir, file_lock
from service_lifecycle import (DEFAULT_PORT, LAUNCH_AGENT_LABEL, MODE_FILE,
                               activation_marker, build_launch_agent, read_service_mode)

SOURCE_ROOT = Path(__file__).absolute().parent.parent
RUNTIME_NAME = "browser-runtime"
RUNTIME_OWNER = ".usage-meter-runtime.json"
RUNTIME_FILES = (
    "scripts/meter.py", "scripts/usage_log.py", "scripts/pricing.py", "scripts/periods.py",
    "scripts/conversations.py", "scripts/platform_support.py", "scripts/rpc_transport.py", "scripts/official_credits.py",
    "scripts/service_lifecycle.py", "scripts/browser_access.py", "web/index.html",
    ".codex-plugin/plugin.json",
)
_OWNER_BYTES = b'{"owner":"local.codex-usage-meter","format":1}\n'


def _require_macos():
    if sys.platform != "darwin":
        raise RuntimeError("unsupported：按需浏览器访问仅支持 macOS；其他系统保持原有启动方式")


def _paths(folder):
    home = Path.home()
    return (home / "Library" / "LaunchAgents" / (LAUNCH_AGENT_LABEL + ".plist"),
            Path(folder) / RUNTIME_NAME / "scripts" / "meter.py")


def _directory(path):
    details = path.lstat()
    if (not stat.S_ISDIR(details.st_mode) or
            getattr(details, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
        raise RuntimeError("运行副本需要普通目录，不能是链接或其他对象")


def _runtime_payload(root, *, managed=False):
    """Read an exact code allowlist; never copy hook/MCP settings or user data."""
    try:
        _directory(root)
    except FileNotFoundError:
        if managed:
            return None
        raise RuntimeError("插件运行文件不完整，请重新安装完整源码") from None
    if managed:
        if _read_existing(root / RUNTIME_OWNER, 1024) != _OWNER_BYTES:
            raise RuntimeError("目标运行目录不属于本插件，未覆盖任何文件")
        allowed = set(RUNTIME_FILES) | {RUNTIME_OWNER}
        directories = {str(Path(name).parent) for name in RUNTIME_FILES}
        # A direct Python invocation can create bytecode in this owned directory.
        directories.add("scripts/__pycache__")
        modules = {Path(name).stem for name in RUNTIME_FILES if name.startswith("scripts/")}
        for directory, subdirs, filenames in os.walk(root, followlinks=False):
            for name in subdirs + filenames:
                path = Path(directory) / name
                relative = path.relative_to(root).as_posix()
                details = path.lstat()
                if (stat.S_ISLNK(details.st_mode) or getattr(details, "st_file_attributes", 0)
                        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
                    raise RuntimeError("运行副本含链接，未覆盖任何文件")
                if stat.S_ISDIR(details.st_mode) and relative in directories:
                    continue
                bytecode = (path.parent.relative_to(root).as_posix() == "scripts/__pycache__"
                            and re.fullmatch(r"([a-z_]+)\.cpython-[0-9]+(?:\.opt-[0-9]+)?\.pyc", name))
                if (not stat.S_ISREG(details.st_mode) or
                        relative not in allowed and not (bytecode and bytecode[1] in modules)):
                    raise RuntimeError("运行副本含非预期文件，未覆盖任何文件")
    payload = {}
    # Existing 0.9.x runtimes predate official credit tracking. Accept their
    # exact owned payload for atomic replacement/rollback, without accepting a
    # missing module from a runtime that should already contain it.
    legacy_credits = False
    if managed:
        try:
            old_manifest = json.loads(_read_existing(root / ".codex-plugin/plugin.json", 32768) or b"{}")
            old_version = old_manifest.get("version", "").split("+")[0]
            legacy_credits = old_manifest.get("name") == "codex-usage-meter" and bool(re.fullmatch(r"0\.[0-9]\.[0-9]+", old_version))
        except (ValueError, AttributeError):
            pass
    for name in RUNTIME_FILES:
        relative = Path(name)
        _directory(root / relative.parent)
        raw = _read_existing(root / relative, 2 * 1024 * 1024)
        if raw is None:
            if managed and legacy_credits and name == "scripts/official_credits.py":
                continue
            raise RuntimeError("插件运行文件不完整，请重新安装完整源码")
        payload[name] = raw
    try:
        manifest = json.loads(payload[".codex-plugin/plugin.json"])
        if not isinstance(manifest, dict) or manifest.get("name") != "codex-usage-meter":
            raise ValueError
    except (ValueError, UnicodeError):
        raise RuntimeError("运行副本的插件标识无效") from None
    payload[RUNTIME_OWNER] = _OWNER_BYTES
    return payload


def _stage_runtime(folder, payload):
    staged = Path(tempfile.mkdtemp(prefix=".browser-runtime-stage-", dir=folder))
    try:
        for name, raw in payload.items():
            _write(staged / name, raw)
        return staged
    except BaseException:
        shutil.rmtree(staged)
        raise


def _target():
    return "gui/" + str(os.getuid())


def _launchctl(*arguments):
    try:
        return subprocess.run(["/bin/launchctl", *arguments], capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=20,
                              check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("无法完成本插件的按需服务管理操作") from None


def _checked(*arguments):
    if _launchctl(*arguments).returncode != 0:
        raise RuntimeError("系统未确认本插件的按需服务操作；未修改 Codex 权限设置")


def _job_status():
    result = _launchctl("print", _target() + "/" + LAUNCH_AGENT_LABEL)
    match = re.search(r"(?m)^\s*pid\s*=\s*([1-9][0-9]*)\s*$", result.stdout) if result.returncode == 0 else None
    return {"loaded": result.returncode == 0, "workerPid": int(match[1]) if match else None}


def _read_existing(path, limit=65536):
    """Read one exact regular file, without following links or copying unbounded data."""
    try:
        original = path.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(original.st_mode) or original.st_size > limit or
            getattr(original, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
        raise RuntimeError("本插件的本地配置文件类型或大小无效")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (not stat.S_ISREG(opened.st_mode) or
                (opened.st_dev, opened.st_ino) != (original.st_dev, original.st_ino)):
            raise RuntimeError("本插件的本地配置发生变化，请重试")
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise RuntimeError("本插件的本地配置文件过大")
    return raw


def _write(path, content):
    """Atomically write private local configuration, or restore an absent file."""
    if content is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".meter-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _local_http_port(value):
    if not isinstance(value, str) or any(ord(char) < 33 or ord(char) == 127 for char in value):
        raise ValueError("需要不含凭据或路径的本地 HTTP 地址")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or
                parsed.username is not None or parsed.password is not None or
                parsed.port is None or not 1 <= parsed.port <= 65535 or
                parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError
        return parsed.port
    except (ValueError, TypeError):
        raise ValueError("仅接受 http://127.0.0.1:端口/，不能包含凭据、查询或额外路径") from None


def endpoint_port(folder):
    raw = _read_existing(Path(folder) / "endpoint.json", 4096)
    if raw is None:
        return None
    try:
        endpoint = json.loads(raw)
        return _local_http_port(endpoint.get("url"))
    except (ValueError, TypeError, AttributeError, UnicodeError):
        raise RuntimeError("保存的本地面板地址无效，请先修复地址配置") from None


def _wait_for_port(port, timeout=45):
    # macOS can retain recently closed launchd connections in TIME_WAIT after
    # the owning job has exited. Keep the address and allow bounded cleanup;
    # never steal an occupied port or choose another one during an upgrade.
    deadline = time.monotonic() + timeout
    while True:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("127.0.0.1", port))
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise RuntimeError("本地面板端口尚未释放；没有停止其他程序，请稍后重试") from None
            time.sleep(0.1)


def _owned_plist(path):
    raw = _read_existing(path)
    if raw is not None:
        try:
            config = plistlib.loads(raw)
            if not isinstance(config, dict) or config.get("Label") != LAUNCH_AGENT_LABEL:
                raise ValueError
        except (ValueError, TypeError, plistlib.InvalidFileException):
            raise RuntimeError("目标服务文件不属于本插件，未覆盖或移除") from None
    return raw


def status(folder):
    if sys.platform != "darwin":
        return {"supported": False, "loaded": False, "workerPid": None,
                "port": None, "mode": "unsupported"}
    marker = read_service_mode(folder)
    port = marker["port"] if marker else endpoint_port(folder)
    return {"supported": True, **_job_status(), "port": port,
            "mode": marker["mode"] if marker else "manual"}


def install(folder, *, port=None, proxy_http=None):
    _require_macos()
    folder = Path(folder).expanduser().resolve()
    with file_lock(folder / "browser-access.lock"):
        return _install(folder, port=port, proxy_http=proxy_http)


def _install(folder, *, port=None, proxy_http=None):
    plist_path, meter_script = _paths(folder)
    runtime = folder / RUNTIME_NAME
    payload = _runtime_payload(SOURCE_ROOT)
    previous_payload = _runtime_payload(runtime, managed=True)
    runtime_changed = payload != previous_payload
    previous_plist = _owned_plist(plist_path)
    marker_path = folder / MODE_FILE
    previous_marker = _read_existing(marker_path, 4096)
    old_mode = read_service_mode(folder)
    saved_port = endpoint_port(folder)
    chosen_port = port if port is not None else saved_port or (old_mode or {}).get("port") or DEFAULT_PORT
    if saved_port is not None and port is not None and saved_port != port:
        raise RuntimeError("指定端口与保存的面板地址不一致；请保留已有地址")
    config = build_launch_agent(sys.executable, meter_script, folder, port=chosen_port)
    marker = activation_marker(chosen_port)
    if proxy_http is not None:
        proxy = "http://127.0.0.1:" + str(_local_http_port(proxy_http)) + "/"
        config["EnvironmentVariables"] = {"HTTP_PROXY": proxy, "HTTPS_PROXY": proxy,
                                          "NO_PROXY": "localhost,127.0.0.1,::1"}
    elif previous_plist is not None:
        # Omitting the option preserves the user's explicit proxy preference.
        # Revalidate each saved URL; never copy arbitrary inherited environment.
        previous_environment = plistlib.loads(previous_plist).get("EnvironmentVariables", {})
        if not isinstance(previous_environment, dict):
            raise RuntimeError("已有按需服务的代理配置无效，未修改原配置")
        proxies = {name: "http://127.0.0.1:" + str(_local_http_port(previous_environment[name])) + "/"
                   for name in ("HTTP_PROXY", "HTTPS_PROXY") if name in previous_environment}
        if proxies:
            config["EnvironmentVariables"] = {**proxies, "NO_PROXY": "localhost,127.0.0.1,::1"}
    plist_bytes = plistlib.dumps(config, sort_keys=True)
    old_job = _job_status()
    if old_job["loaded"] and previous_plist is None:
        raise RuntimeError("本插件服务已载入，但服务文件缺失；请先检查或卸载此服务")
    if old_job["loaded"] and previous_plist == plist_bytes and old_mode == marker and not runtime_changed:
        return {"supported": True, **old_job, "port": chosen_port, "mode": marker["mode"]}
    staged = _stage_runtime(folder, payload) if runtime_changed else None
    backup = None
    runtime_installed = committed = False
    unloaded = bootstrap_attempted = bootstrapped = False
    try:
        if old_job["loaded"]:
            _checked("bootout", _target() + "/" + LAUNCH_AGENT_LABEL)
            unloaded = True
        else:
            # Existing meter verifies app identity, PID and its local CSRF token.
            from meter import stop_service
            stop_service(folder)
        _wait_for_port(chosen_port)
        folder.mkdir(parents=True, exist_ok=True)
        if staged is not None:
            if previous_payload is not None:
                backup = Path(tempfile.mkdtemp(prefix=".browser-runtime-backup-", dir=folder))
                backup.rmdir()
                os.replace(runtime, backup)
            os.replace(staged, runtime)
            staged = None
            runtime_installed = True
        _write(plist_path, plist_bytes)
        bootstrap_attempted = True
        _checked("bootstrap", _target(), str(plist_path))
        bootstrapped = True
        _write(marker_path, (json.dumps(marker, ensure_ascii=False) + "\n").encode("utf-8"))
        committed = True
    except Exception:
        recovered = True
        if bootstrap_attempted:
            try:
                if bootstrapped or _job_status()["loaded"]:
                    _checked("bootout", _target() + "/" + LAUNCH_AGENT_LABEL)
            except RuntimeError:
                recovered = False
        try:
            if runtime_installed:
                shutil.rmtree(runtime)
            if backup is not None and backup.exists():
                os.replace(backup, runtime)
                backup = None
            _write(plist_path, previous_plist)
            _write(marker_path, previous_marker)
            if unloaded and previous_plist is not None:
                _checked("bootstrap", _target(), str(plist_path))
        except (OSError, RuntimeError):
            recovered = False
        raise RuntimeError("按需访问安装未完成；" + ("原配置已恢复，可重试" if recovered else "恢复未完成，请检查本插件服务状态")) from None
    finally:
        # A failed recovery retains its private backup for an explicit repair.
        for temporary in (staged, backup if committed else None):
            if temporary is not None:
                with contextlib.suppress(OSError):
                    shutil.rmtree(temporary)
    return {"supported": True, "loaded": True, "workerPid": None,
            "port": chosen_port, "mode": marker["mode"]}


def uninstall(folder):
    _require_macos()
    folder = Path(folder).expanduser().resolve()
    with file_lock(folder / "browser-access.lock"):
        return _uninstall(folder)


def _uninstall(folder):
    plist_path, _ = _paths(folder)
    _owned_plist(plist_path)
    _read_existing(folder / MODE_FILE, 4096)
    if _job_status()["loaded"]:
        _checked("bootout", _target() + "/" + LAUNCH_AGENT_LABEL)
    _write(plist_path, None)
    _write(folder / MODE_FILE, None)
    try:
        saved_port = endpoint_port(folder)
    except (OSError, RuntimeError):
        saved_port = None  # Keep malformed endpoint data untouched; opt-out succeeded.
    return {"supported": True, "loaded": False, "workerPid": None,
            "port": saved_port, "mode": "manual"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="管理用量计在 macOS 上的按需浏览器访问")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("install")
    setup.add_argument("--port", type=int)
    setup.add_argument("--proxy-http")
    commands.add_parser("status")
    commands.add_parser("uninstall")
    args = parser.parse_args(argv)
    try:
        result = (install(args.data_dir, port=args.port, proxy_http=args.proxy_http)
                  if args.command == "install" else status(args.data_dir)
                  if args.command == "status" else uninstall(args.data_dir))
    except (RuntimeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    except OSError:
        # Never expose launchctl output, inherited environment or local file paths.
        print(json.dumps({"ok": False, "error": "按需访问操作未完成；请检查系统支持、安装状态、端口和本地配置。"}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
