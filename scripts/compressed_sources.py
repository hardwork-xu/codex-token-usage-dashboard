"""Recover only the exact compressed sibling of an already registered transcript.

This module never scans for sessions or changes the source. Decoded JSONL is a
private, disposable input cache; the caller releases it after durable numeric
archiving. All errors are fixed codes and never include transcript content.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import queue
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time

from platform_support import file_lock, subprocess_options

MAX_COMPRESSED_BYTES = 512 * 1024 * 1024
MAX_DECODED_BYTES = 2 * 1024 * 1024 * 1024
MAX_HEADER_BYTES = 4 * 1024 * 1024
DECODE_TIMEOUT = 30.0
_CHUNK = 64 * 1024
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class CompressedSourceError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _regular(info):
    return (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            and not getattr(info, "st_file_attributes", 0) & 0x400)


def _stamp(info):
    return {"device": info.st_dev, "inode": info.st_ino, "size": info.st_size,
            "mtimeNs": info.st_mtime_ns, "ctimeNs": info.st_ctime_ns}


def _safe_ancestors(path):
    for parent in path.parents:
        try:
            info = parent.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise CompressedSourceError("invalid_source") from None
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise CompressedSourceError("invalid_source")


def _open_regular(path):
    """Compare path and opened-file identities; do not follow final symlinks."""
    handle = None
    try:
        before = path.lstat()
        if not _regular(before):
            raise CompressedSourceError("invalid_source")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_BINARY", 0))
        handle = os.fdopen(descriptor, "rb")
        if _stamp(before) != _stamp(os.fstat(handle.fileno())) or _stamp(before) != _stamp(path.lstat()):
            handle.close()
            raise CompressedSourceError("source_changed")
        return handle
    except CompressedSourceError:
        if handle is not None:
            handle.close()
        raise
    except OSError:
        if handle is not None:
            handle.close()
        raise CompressedSourceError("invalid_source") from None


def _decoder_command():
    for module, backend in (("compression.zstd", "stdlib"), ("zstandard", "zstandard")):
        try:
            available = importlib.util.find_spec(module) is not None
        except (ModuleNotFoundError, ValueError, ImportError):
            available = False
        if available:
            return [sys.executable, str(Path(__file__).resolve()), "--decode", backend]
    executable = shutil.which("zstd.exe" if sys.platform == "win32" else "zstd")
    if not executable and sys.platform == "darwin":
        for candidate in ("/opt/homebrew/bin/zstd", "/usr/local/bin/zstd"):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                executable = candidate
                break
    if executable and (sys.platform != "win32" or Path(executable).suffix.lower() == ".exe"):
        return [executable, "-d", "-c", "--quiet", "--memory=128MB"]
    raise CompressedSourceError("decoder_unavailable")


def _validate_header(raw, thread_id):
    try:
        record = json.loads(raw)
        payload = record.get("payload")
        identity = payload.get("id") if isinstance(payload, dict) else None
        if record.get("type") != "session_meta" or identity != thread_id:
            raise CompressedSourceError("identity_mismatch")
    except (ValueError, UnicodeError, AttributeError):
        raise CompressedSourceError("identity_mismatch") from None


def _decode(source, destination, thread_id):
    """Pipe bounded chunks through a deadline-enforced decoder subprocess."""
    command = _decoder_command()
    process = None
    chunks = queue.Queue(maxsize=2)
    stop = threading.Event()
    thread = None
    try:
        process = subprocess.Popen(command, stdin=source, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, **subprocess_options())

        def read_pipe():
            try:
                while not stop.is_set():
                    data = process.stdout.read(_CHUNK)
                    while not stop.is_set():
                        try:
                            chunks.put(data, timeout=0.05)
                            break
                        except queue.Full:
                            pass
                    if not data:
                        return
            except (OSError, ValueError):
                while not stop.is_set():
                    try:
                        chunks.put(None, timeout=0.05)
                        return
                    except queue.Full:
                        pass

        thread = threading.Thread(target=read_pipe, daemon=True)
        thread.start()
        deadline = time.monotonic() + DECODE_TIMEOUT
        total = 0
        first_line = bytearray()
        checked = False
        digest = hashlib.sha256()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CompressedSourceError("decode_timeout")
            try:
                chunk = chunks.get(timeout=remaining)
            except queue.Empty:
                raise CompressedSourceError("decode_timeout") from None
            if chunk is None:
                raise CompressedSourceError("decode_failed")
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_DECODED_BYTES:
                raise CompressedSourceError("decode_limit")
            if not checked:
                head, newline, _rest = chunk.partition(b"\n")
                if len(first_line) + len(head) > MAX_HEADER_BYTES:
                    raise CompressedSourceError("decode_limit")
                first_line.extend(head)
                if newline:
                    _validate_header(first_line, thread_id)
                    checked = True
                    first_line.clear()
            destination.write(chunk)
            digest.update(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CompressedSourceError("decode_timeout")
        try:
            if process.wait(timeout=remaining) != 0:
                raise CompressedSourceError("decode_failed")
        except subprocess.TimeoutExpired:
            raise CompressedSourceError("decode_timeout") from None
        if not checked:
            _validate_header(first_line, thread_id)
        destination.flush()
        os.fsync(destination.fileno())
        return digest.hexdigest()
    except CompressedSourceError:
        raise
    except (OSError, ValueError):
        raise CompressedSourceError("decode_failed") from None
    finally:
        stop.set()
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            if thread is not None:
                thread.join(timeout=1)
            if process.stdout is not None:
                process.stdout.close()


class CompressedSourceCache:
    def __init__(self, cache_dir):
        self.folder = Path(cache_dir).absolute()

    @staticmethod
    def _paths(thread_id, original_path):
        if not isinstance(thread_id, str) or not _ID.fullmatch(thread_id):
            raise CompressedSourceError("invalid_source")
        original = Path(original_path).absolute()
        if original.suffix != ".jsonl":
            raise CompressedSourceError("invalid_source")
        key = hashlib.sha256((thread_id + "\0" + str(original)).encode()).hexdigest()
        return original, Path(str(original) + ".zst"), key

    def inspect(self, thread_id, original_path):
        original, compressed, _key = self._paths(thread_id, original_path)
        try:
            original.lstat()
            return None  # Plain paths, even invalid ones, remain the normal reader's responsibility.
        except FileNotFoundError:
            pass
        except OSError:
            raise CompressedSourceError("invalid_source") from None
        _safe_ancestors(compressed)
        try:
            info = compressed.lstat()
        except FileNotFoundError:
            return None
        except OSError:
            raise CompressedSourceError("invalid_source") from None
        if not _regular(info):
            raise CompressedSourceError("invalid_source")
        if info.st_size > MAX_COMPRESSED_BYTES:
            raise CompressedSourceError("decode_limit")
        return {"format": "zstd-v1", "pathHash": hashlib.sha256(str(compressed).encode()).hexdigest(),
                **_stamp(info)}

    def _prepare(self):
        try:
            try:
                _safe_ancestors(self.folder)
            except CompressedSourceError:
                raise CompressedSourceError("invalid_cache") from None
            self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = self.folder.lstat()
            if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & 0x400):
                raise CompressedSourceError("invalid_cache")
            if os.name != "nt":
                os.chmod(self.folder, 0o700)
            lock = self.folder / "cache.lock"
            if lock.exists() or lock.is_symlink():
                if not _regular(lock.lstat()):
                    raise CompressedSourceError("invalid_cache")
            return lock
        except OSError:
            raise CompressedSourceError("invalid_cache") from None

    def _discard(self, key):
        # Only exact key-owned files are eligible. Never unlink an unexpected link.
        for path in (self.folder / (key + ".jsonl"), self.folder / (key + ".json")):
            try:
                if not _regular(path.lstat()):
                    raise CompressedSourceError("invalid_cache")
                path.unlink()
            except FileNotFoundError:
                pass
        for path in self.folder.glob(key + ".*.tmp"):
            try:
                if _regular(path.lstat()):
                    path.unlink()
            except FileNotFoundError:
                pass

    def _cached(self, key, stamp, thread_id):
        path = self.folder / (key + ".jsonl")
        manifest = self.folder / (key + ".json")
        try:
            with _open_regular(manifest) as handle:
                raw = handle.read(4097)
            if len(raw) > 4096:
                return None
            saved = json.loads(raw)
            if (not isinstance(saved, dict) or set(saved) != {"sourceStamp", "decodedStamp", "sha256"}
                    or saved["sourceStamp"] != stamp or not isinstance(saved["sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", saved["sha256"])):
                return None
            with _open_regular(path) as handle:
                if _stamp(os.fstat(handle.fileno())) != saved["decodedStamp"]:
                    return None
                raw = handle.readline(MAX_HEADER_BYTES + 1)
                if len(raw) > MAX_HEADER_BYTES:
                    return None
                _validate_header(raw, thread_id)
            return path
        except (OSError, ValueError, TypeError):
            return None

    def resolve(self, thread_id, original_path):
        stamp = self.inspect(thread_id, original_path)
        if stamp is None:
            return None
        _original, compressed, key = self._paths(thread_id, original_path)
        lock = self._prepare()
        try:
            with file_lock(lock, timeout=DECODE_TIMEOUT + 5):
                if self.inspect(thread_id, original_path) != stamp:
                    raise CompressedSourceError("source_changed")
                cached = self._cached(key, stamp, thread_id)
                if cached is not None:
                    if self.inspect(thread_id, original_path) != stamp:
                        self._discard(key)
                        raise CompressedSourceError("source_changed")
                    return {"path": str(cached), "sourceStamp": stamp, "reused": True,
                            "compressedPath": str(compressed)}
                self._discard(key)
                temporary = None
                try:
                    with _open_regular(compressed) as source:
                        if _stamp(os.fstat(source.fileno())) != {k: stamp[k] for k in _stamp(os.fstat(source.fileno()))}:
                            raise CompressedSourceError("source_changed")
                        fd, name = tempfile.mkstemp(prefix=key + ".", suffix=".tmp", dir=self.folder)
                        temporary = Path(name)
                        with os.fdopen(fd, "wb") as output:
                            digest = _decode(source, output, thread_id)
                        if self.inspect(thread_id, original_path) != stamp:
                            raise CompressedSourceError("source_changed")
                    path = self.folder / (key + ".jsonl")
                    os.replace(temporary, path)
                    temporary = None
                    manifest = {"sourceStamp": stamp, "decodedStamp": _stamp(path.lstat()), "sha256": digest}
                    fd, name = tempfile.mkstemp(prefix=key + ".", suffix=".tmp", dir=self.folder)
                    temporary = Path(name)
                    with os.fdopen(fd, "w", encoding="utf-8") as output:
                        json.dump(manifest, output, separators=(",", ":"))
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(temporary, self.folder / (key + ".json"))
                    temporary = None
                    return {"path": str(path), "sourceStamp": stamp, "reused": False,
                            "compressedPath": str(compressed)}
                except Exception:
                    self._discard(key)
                    raise
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
        except CompressedSourceError:
            raise
        except (OSError, TimeoutError):
            raise CompressedSourceError("invalid_cache") from None

    def release(self, thread_id, original_path, source_stamp=None):
        _original, _compressed, key = self._paths(thread_id, original_path)
        if not self.folder.exists():
            return
        lock = self._prepare()
        with file_lock(lock, timeout=DECODE_TIMEOUT + 5):
            if source_stamp is not None:
                try:
                    with _open_regular(self.folder / (key + ".json")) as stream:
                        manifest = json.loads(stream.read(4097))
                    if manifest.get("sourceStamp") != source_stamp:
                        return
                except (OSError, ValueError, AttributeError):
                    return
            self._discard(key)


def _worker(backend):
    # Runs as a bounded subprocess; no transcript content is sent to stderr.
    try:
        if backend == "stdlib":
            import compression.zstd as zstd
            with zstd.ZstdFile(sys.stdin.buffer, "rb", options={zstd.DecompressionParameter.window_log_max: 27}) as stream:
                shutil.copyfileobj(stream, sys.stdout.buffer, _CHUNK)
        elif backend == "zstandard":
            import zstandard
            # stream_reader accepts some truncated frames at EOF. The object API
            # exposes eof, so require a completed frame and handle concatenation.
            decoder = zstandard.ZstdDecompressor(max_window_size=128 * 1024)
            obj = decoder.decompressobj()
            completed = False
            while True:
                data = sys.stdin.buffer.read(128)
                if not data:
                    if not completed:
                        return 1
                    break
                while data:
                    if completed:
                        obj = decoder.decompressobj()
                    sys.stdout.buffer.write(obj.decompress(data))
                    completed = obj.eof
                    data = obj.unused_data if completed else b""
        else:
            return 2
        sys.stdout.buffer.flush()
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(_worker(sys.argv[2]) if len(sys.argv) == 3 and sys.argv[1] == "--decode" else 2)
