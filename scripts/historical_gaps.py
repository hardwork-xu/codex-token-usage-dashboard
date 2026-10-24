"""Private acknowledgements of explicitly selected, genuinely absent logs.

The ledger never stores paths, transcript content, usage or estimated money.
An acknowledgement is not evidence of zero usage or complete account history.
"""
from __future__ import annotations

from contextlib import contextmanager, suppress
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import time

from platform_support import file_lock, is_windows

MAX_ENTRIES = 4096
MAX_SELECTION = 100
MAX_LEDGER_BYTES = 1024 * 1024
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


class HistoricalGapError(ValueError):
    """Stable, content-free failure suitable for displaying in the local UI."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _identifier(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _timestamp(value):
    return type(value) in (int, float) and 0 <= value <= 253402300799 and math.isfinite(value)


def _ordinary(info, *, directory=False):
    return ((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
            and not stat.S_ISLNK(info.st_mode)
            and not getattr(info, "st_file_attributes", 0) & 0x400)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _clean_ledger(value):
    if (not isinstance(value, dict) or set(value) != {"version", "acknowledged"}
            or type(value["version"]) is not int or value["version"] != 1
            or not isinstance(value["acknowledged"], dict) or len(value["acknowledged"]) > MAX_ENTRIES):
        raise HistoricalGapError("ledger_invalid")
    for thread_id, row in value["acknowledged"].items():
        if (not _identifier(thread_id) or not isinstance(row, dict)
                or set(row) != {"pathHash", "acknowledgedAt"}
                or not isinstance(row["pathHash"], str) or not _HASH.fullmatch(row["pathHash"])
                or not _timestamp(row["acknowledgedAt"])):
            raise HistoricalGapError("ledger_invalid")
    return value


def _registered_path(record):
    value = record.get("path") if isinstance(record, dict) else None
    if (not isinstance(value, str) or not value or len(value) > 32768 or "\0" in value
            or any(0xD800 <= ord(character) <= 0xDFFF for character in value)):
        raise HistoricalGapError("registration_invalid")
    try:
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError
        return path.absolute()
    except (OSError, ValueError, RuntimeError):
        raise HistoricalGapError("registration_invalid") from None


def _path_hash(path):
    return hashlib.sha256(os.path.normcase(str(path)).encode("utf-8")).hexdigest()


def _missing(path):
    """Only two actual ENOENT results qualify; all other failures stay active."""
    for candidate in (path, Path(str(path) + ".zst")):
        try:
            candidate.lstat()
            return False
        except FileNotFoundError:
            pass
        except OSError:
            raise HistoricalGapError("source_unreadable") from None
    return True


class HistoricalGaps:
    def __init__(self, folder):
        self.folder = Path(folder).expanduser().absolute()
        self.path = self.folder / "historical-gaps.json"
        self.lock_path = self.folder / "historical-gaps.lock"

    def _check_folder(self):
        for parent in (self.folder, *self.folder.parents):
            try:
                info = parent.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                raise HistoricalGapError("ledger_unavailable") from None
            if not _ordinary(info, directory=True):
                raise HistoricalGapError("ledger_unavailable")

    @contextmanager
    def _locked(self):
        self._check_folder()
        try:
            self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            for path in (self.path, self.lock_path):
                try:
                    if not _ordinary(path.lstat()):
                        raise HistoricalGapError("ledger_unavailable")
                except FileNotFoundError:
                    pass
            with file_lock(self.lock_path):
                yield
        except (OSError, TimeoutError):
            raise HistoricalGapError("ledger_unavailable") from None

    def load(self):
        """Read a strict bounded ledger; absence is empty, corruption is an error."""
        self._check_folder()
        handle = None
        try:
            try:
                before = self.path.lstat()
            except FileNotFoundError:
                return {"version": 1, "acknowledged": {}}
            if not _ordinary(before) or before.st_size > MAX_LEDGER_BYTES:
                raise HistoricalGapError("ledger_invalid")
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
            handle = os.fdopen(fd, "rb")
            opened = os.fstat(handle.fileno())
            if not _ordinary(opened) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise HistoricalGapError("ledger_invalid")
            raw = handle.read(MAX_LEDGER_BYTES + 1)
            if len(raw) > MAX_LEDGER_BYTES:
                raise HistoricalGapError("ledger_invalid")
            return _clean_ledger(json.loads(raw, object_pairs_hook=_unique_object))
        except (OSError, TimeoutError):
            raise HistoricalGapError("ledger_unavailable") from None
        except (ValueError, TypeError, RecursionError, OverflowError) as exc:
            if isinstance(exc, HistoricalGapError):
                raise
            raise HistoricalGapError("ledger_invalid") from None
        finally:
            if handle is not None:
                handle.close()

    def _write(self, ledger):
        encoded = (json.dumps(_clean_ledger(ledger), sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) > MAX_LEDGER_BYTES:
            raise HistoricalGapError("ledger_invalid")
        temporary = None
        try:
            fd, name = tempfile.mkstemp(prefix="historical-gaps.", suffix=".tmp", dir=self.folder)
            temporary = Path(name)
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            for attempt in range(11):
                try:
                    os.replace(temporary, self.path)
                    break
                except PermissionError:
                    if not is_windows() or attempt == 10:
                        raise
                    time.sleep(0.05)
        except OSError:
            raise HistoricalGapError("ledger_unavailable") from None
        finally:
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)

    def _inspect(self, registry, ledger):
        if not isinstance(registry, dict):
            raise HistoricalGapError("registry_invalid")
        missing, errors, hashes = set(), [], {}
        retained = dict(ledger["acknowledged"])
        for thread_id in list(retained):
            if thread_id not in registry:
                del retained[thread_id]
        for thread_id, record in registry.items():
            if not _identifier(thread_id):
                raise HistoricalGapError("registry_invalid")
            try:
                path = _registered_path(record)
                path_hash = hashes[thread_id] = _path_hash(path)
                if thread_id in retained and retained[thread_id]["pathHash"] != path_hash:
                    del retained[thread_id]
                if _missing(path):
                    missing.add(thread_id)
                else:
                    retained.pop(thread_id, None)
            except HistoricalGapError as exc:
                errors.append({"threadId": thread_id, "code": exc.code})
        return {"version": 1, "acknowledged": retained}, missing, hashes, errors

    @staticmethod
    def _saved(archive, thread_id):
        try:
            saved = archive.load(thread_id)
        except Exception:
            raise HistoricalGapError("archive_unreadable") from None
        if saved is None:
            return None
        # UsageArchive.load validates the numeric schema and content hash. Do not
        # infer zero spending from missing, invalid or aggregate-free records.
        if not isinstance(saved, dict) or not _timestamp(saved.get("savedAt")) or not isinstance(saved.get("result"), dict):
            raise HistoricalGapError("archive_unreadable")
        if saved["result"].get("threadId") != thread_id or saved["result"].get("identityVerified") is not True:
            raise HistoricalGapError("archive_unreadable")
        return saved

    def _view(self, ledger, missing, errors, archive):
        tasks = []
        errors = list(errors)
        candidates = []
        for thread_id in sorted(missing):
            try:
                saved = self._saved(archive, thread_id)
            except HistoricalGapError as exc:
                errors.append({"threadId": thread_id, "code": exc.code})
                continue
            if thread_id not in ledger["acknowledged"]:
                candidates.append(thread_id)
                continue
            has_usage = saved is not None and any(
                isinstance(turn.get("tokens"), dict) for turn in saved["result"].get("turns", []))
            tasks.append({"threadId": thread_id, "acknowledgedAt": ledger["acknowledged"][thread_id]["acknowledgedAt"],
                          "hasSavedUsage": has_usage, "savedAt": saved["savedAt"] if saved else None})
        saved_count = sum(row["hasSavedUsage"] for row in tasks)
        return {"status": "error" if errors else "ready", "acknowledgedTasks": len(tasks),
                "savedTasks": saved_count, "unknownCostTasks": len(tasks) - saved_count,
                "unreadableTasks": len(tasks),
                "updatedAt": max((row["acknowledgedAt"] for row in tasks), default=None),
                "tasks": tasks, "candidateThreadIds": candidates,
                "validationErrors": errors, "errorCount": len(errors)}

    def reconcile(self, registry, archive):
        """Revoke acknowledgements as soon as source paths change or reappear."""
        with self._locked():
            previous = self.load()
            ledger, missing, _hashes, errors = self._inspect(registry, previous)
            if ledger != previous:
                self._write(ledger)
            return self._view(ledger, missing, errors, archive)

    def acknowledge(self, registry, selected_thread_ids, archive, *, now=None):
        """Acknowledge only explicit IDs, atomically; new missing logs stay active."""
        if (not isinstance(selected_thread_ids, (list, tuple)) or not selected_thread_ids
                or len(selected_thread_ids) > MAX_SELECTION
                or any(not _identifier(value) for value in selected_thread_ids)
                or len(set(selected_thread_ids)) != len(selected_thread_ids)):
            raise HistoricalGapError("selection_invalid")
        acknowledged_at = time.time() if now is None else now
        if not _timestamp(acknowledged_at):
            raise HistoricalGapError("timestamp_invalid")
        with self._locked():
            previous = self.load()
            ledger, missing, hashes, errors = self._inspect(registry, previous)
            # Revocations reflect newly observed source state even when a later
            # selection fails. Otherwise a brief reappearance could leave an old
            # acknowledgement active after the source disappears a second time.
            if ledger != previous:
                self._write(ledger)
                previous = {"version": 1, "acknowledged": dict(ledger["acknowledged"])}
            for thread_id in selected_thread_ids:
                if thread_id not in registry:
                    raise HistoricalGapError("registration_invalid")
                if thread_id not in missing:
                    code = next((row["code"] for row in errors if row["threadId"] == thread_id), "source_not_missing")
                    raise HistoricalGapError(code)
                self._saved(archive, thread_id)  # Corruption is not an unknown-price zero.
                ledger["acknowledged"].setdefault(thread_id, {"pathHash": hashes[thread_id], "acknowledgedAt": acknowledged_at})
            if len(ledger["acknowledged"]) > MAX_ENTRIES:
                raise HistoricalGapError("ledger_limit")
            if ledger != previous:
                self._write(ledger)
            return self._view(ledger, missing, errors, archive)
