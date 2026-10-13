"""Private, durable snapshots of already verified, normalized usage metadata.

This is not a log discovery mechanism or a billing ledger. Only a strict numeric
and identity schema is retained; messages, paths, account data and price totals
are never copied. A snapshot replaces another snapshot atomically. Conflicting
versions are never added together or spliced into a fabricated history.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time


MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_TURNS = 100_000
MAX_SEGMENTS = 200_000
MAX_COUNTER = 2**63 - 1
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_COUNTERS = ("total", "input", "cachedInput", "cacheWriteInput", "output", "reasoningOutput")
_ERROR = "本地用量存档无法读写；请检查存储空间与文件权限。"
_CORRUPT = "本地用量存档校验失败；此存档未用于统计。"


class UsageArchiveError(ValueError):
    """A content-free failure, safe to display without exposing archived data."""


def _invalid():
    raise UsageArchiveError("用量记录不符合本地存档格式；未写入存档。")


def _identifier(value, *, nullable=False):
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not _ID.fullmatch(value):
        _invalid()
    return value


def _text(value, limit):
    if (not isinstance(value, str) or len(value) > limit
            or any((ord(c) < 32 and c not in "\n\t") or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        _invalid()
    return value


def _number(value, *, nullable=False, maximum=253402300799):
    if nullable and value is None:
        return None
    if type(value) not in (int, float) or not 0 <= value <= maximum or not math.isfinite(value):
        _invalid()
    return value


def _integer(value):
    if type(value) is not int or not 0 <= value <= MAX_COUNTER:
        _invalid()
    return value


def _bool(value):
    if type(value) is not bool:
        _invalid()
    return value


def _enum(value, choices):
    if value not in choices:
        _invalid()
    return value


def _day(value):
    if value is None:
        return None
    try:
        if not isinstance(value, str) or len(value) != 10 or date.fromisoformat(value).isoformat() != value:
            _invalid()
    except (ValueError, TypeError):
        _invalid()
    return value


def _tokens(value, *, nullable=False):
    if nullable and value is None:
        return None
    if not isinstance(value, dict):
        _invalid()
    result = {key: _integer(value.get(key)) for key in _COUNTERS}
    if (result["total"] != result["input"] + result["output"]
            or result["cachedInput"] > result["input"]
            or result["reasoningOutput"] > result["output"]):
        _invalid()
    return result


def _pricing(value):
    result = {}
    for key in ("model", "serviceTier"):
        if key in value:
            result[key] = _identifier(value[key], nullable=True)
    if "pricingMetadataStatus" in value:
        result["pricingMetadataStatus"] = _enum(value["pricingMetadataStatus"], ("known", "unknown", "mixed"))
    return result


def _slice(value, *, segment=False):
    if not isinstance(value, dict):
        _invalid()
    result = _pricing(value)
    # Missing metadata remains explicitly unknown, never implicitly Standard.
    for key in ("model", "serviceTier", "pricingMetadataStatus"):
        if key not in result:
            _invalid()
    if segment:
        result["timestamp"] = _number(value.get("timestamp"), nullable=True)
    else:
        result["day"] = _day(value.get("day"))
        if "speedOverrideDay" in value:
            result["speedOverrideDay"] = _day(value["speedOverrideDay"])
    result["apiContext"] = _enum(value.get("apiContext"), (None, "short", "long", "unknown"))
    result["tokens"] = _tokens(value.get("tokens"))
    return result


def _conserves(rows, tokens):
    expected = tokens or dict.fromkeys(_COUNTERS, 0)
    if any(sum(row["tokens"][key] for row in rows) != expected[key] for key in _COUNTERS):
        _invalid()


def _turn(value, thread_id, budget):
    if not isinstance(value, dict) or value.get("threadId") != thread_id:
        _invalid()
    result = {"id": _identifier(value.get("id")), "threadId": thread_id}
    for key in ("startedAt", "endedAt"):
        if key in value:
            result[key] = _number(value[key], nullable=True)
    for key, choices in (("status", ("running", "completed", "interrupted")),
                         ("quality", ("complete", "partial", "unavailable"))):
        if key in value:
            result[key] = _enum(value[key], choices)
    if "note" in value:
        result["note"] = _text(value["note"], 8192)
    if "readingIncomplete" in value:
        result["readingIncomplete"] = _bool(value["readingIncomplete"])
    result["tokens"] = _tokens(value.get("tokens"), nullable=True)
    daily = value.get("dailyUsage")
    if not isinstance(daily, dict) or len(daily) > 36600:
        _invalid()
    result["dailyUsage"] = {}
    for day, tokens in daily.items():
        if day is None:
            _invalid()
        result["dailyUsage"][_day(day)] = _tokens(tokens)
    result["undatedTokens"] = _tokens(value.get("undatedTokens"))
    _conserves([{"tokens": tokens} for tokens in result["dailyUsage"].values()]
               + [{"tokens": result["undatedTokens"]}], result["tokens"])
    for key, segment in (("usageSlices", False), ("usageSegments", True)):
        if key not in value and segment:
            continue  # Legacy snapshots can still preserve already read totals.
        rows = value.get(key)
        if not isinstance(rows, list) or len(rows) > budget[0]:
            _invalid()
        budget[0] -= len(rows)
        result[key] = [_slice(row, segment=segment) for row in rows]
        _conserves(result[key], result["tokens"])
    result.update(_pricing(value))
    return result


def sanitize_result(thread_id, value):
    """Copy only a bounded allowlist; never persist the caller's raw object."""
    _identifier(thread_id)
    if (not isinstance(value, dict) or value.get("threadId") != thread_id
            or value.get("identityVerified") is not True):
        _invalid()
    turns = value.get("turns")
    if not isinstance(turns, list) or len(turns) > MAX_TURNS:
        _invalid()
    budget = [MAX_SEGMENTS]
    result = {"threadId": thread_id, "identityVerified": True,
              "turns": [_turn(turn, thread_id, budget) for turn in turns]}
    if len({turn["id"] for turn in result["turns"]}) != len(result["turns"]):
        _invalid()
    metadata = value.get("conversationMetadata", {})
    if not isinstance(metadata, dict):
        _invalid()
    result["conversationMetadata"] = {}
    if "sourceType" in metadata:
        result["conversationMetadata"]["sourceType"] = _enum(metadata["sourceType"], ("main", "subagent", "unknown"))
    if "parentThreadId" in metadata:
        result["conversationMetadata"]["parentThreadId"] = _identifier(metadata["parentThreadId"], nullable=True)
    if "agentLabel" in metadata:
        result["conversationMetadata"]["agentLabel"] = (None if metadata["agentLabel"] is None
                                                        else _text(metadata["agentLabel"], 120))
    if "reading" in value:
        reading = value["reading"]
        if not isinstance(reading, dict):
            _invalid()
        result["reading"] = {"complete": _bool(reading.get("complete")),
                             "bytesRead": _integer(reading.get("bytesRead")),
                             "fileBytes": _integer(reading.get("fileBytes"))}
        if result["reading"]["bytesRead"] > result["reading"]["fileBytes"]:
            _invalid()
    if "sourceNote" in value:
        result["sourceNote"] = _text(value["sourceNote"], 8192)
    if "sourceScope" in value:
        result["sourceScope"] = _enum(value["sourceScope"], ("historical_snapshot",))
    if "sourceAt" in value:
        result["sourceAt"] = _number(value["sourceAt"])
    if "warnings" in value:
        if not isinstance(value["warnings"], list) or len(value["warnings"]) > 1024:
            _invalid()
        result["warnings"] = [_text(note, 8192) for note in value["warnings"]]
    return result


def _encode(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_SNAPSHOT_BYTES:
        _invalid()
    return encoded


def _regression(previous, current):
    newer = {turn["id"]: turn for turn in current["turns"]}
    older_ids = {turn["id"] for turn in previous["turns"]}
    progressed = any(turn["id"] not in older_ids and turn["tokens"] is not None and any(turn["tokens"].values())
                     for turn in current["turns"])
    for old in previous["turns"]:
        new = newer.get(old["id"])
        if new is None:
            return "turns_missing"
        if old["tokens"] is not None:
            if new["tokens"] is None or any(new["tokens"][key] < old["tokens"][key] for key in _COUNTERS):
                return "tokens_decreased"
            progressed |= any(new["tokens"][key] > old["tokens"][key] for key in _COUNTERS)
        elif new["tokens"] is not None:
            progressed |= any(new["tokens"].values())
        if old.get("usageSegments") and "usageSegments" not in new:
            return "segments_missing"
    if (not progressed and previous.get("reading", {}).get("complete")
            and current.get("reading", {}).get("complete") is False):
        return "scan_incomplete"
    return None


class UsageArchive:
    """One atomically replaced snapshot per verified task, plus tombstones.

    ``load`` returns ``{result, savedAt, contentHash}`` or None. ``save`` returns
    ``{savedAt, retained, unchanged, reason?}``. Retained means the entire prior
    snapshot (or identity tombstone) survived; callers must surface its staleness.
    A tombstone can only be replaced by an explicitly complete verified scan.
    """

    def __init__(self, folder):
        self.folder = Path(folder) / "usage-archive"
        self.path = self.folder / "usage.sqlite3"

    @staticmethod
    def _ordinary(path, *, directory=False):
        info = path.lstat()
        if (not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
                or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise UsageArchiveError(_ERROR)

    @contextmanager
    def _connection(self):
        connection = None
        try:
            self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._ordinary(self.folder, directory=True)
            if os.name != "nt":
                self.folder.chmod(0o700)
            if not self.path.exists():
                try:
                    descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    pass  # A simultaneous first writer still undergoes lstat.
                else:
                    os.close(descriptor)
            self._ordinary(self.path)
            if os.name != "nt":
                self.path.chmod(0o600)
            for suffix in ("-journal", "-wal", "-shm"):
                sibling = Path(str(self.path) + suffix)
                if sibling.exists() or sibling.is_symlink():
                    try:
                        self._ordinary(sibling)
                    except FileNotFoundError:
                        pass  # Another committed transaction removed its journal.
            connection = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA secure_delete=ON")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise UsageArchiveError("本地用量存档版本不受支持；未读取或覆盖存档。")
            connection.execute("BEGIN IMMEDIATE")
            if version == 0:
                connection.execute("CREATE TABLE IF NOT EXISTS snapshots (thread_id TEXT PRIMARY KEY, payload BLOB, content_hash TEXT, saved_at REAL NOT NULL, invalidated INTEGER NOT NULL DEFAULT 0, reason TEXT)")
                connection.execute("PRAGMA user_version=1")
            yield connection
            connection.execute("COMMIT")
        except UsageArchiveError:
            if connection is not None and connection.in_transaction:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
            raise
        except (OSError, sqlite3.Error):
            if connection is not None and connection.in_transaction:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
            raise UsageArchiveError(_ERROR) from None
        finally:
            if connection is not None:
                connection.close()

    def _decode(self, thread_id, row):
        if row is None:
            return None
        payload, digest, saved_at, invalidated, _reason = row
        if invalidated == 1:
            return None
        try:
            if invalidated != 0 or not isinstance(payload, bytes) or len(payload) > MAX_SNAPSHOT_BYTES:
                raise ValueError
            _number(saved_at)
            if not isinstance(digest, str) or hashlib.sha256(payload).hexdigest() != digest:
                raise ValueError
            raw = json.loads(payload)
            result = sanitize_result(thread_id, raw)
            if result != raw or _encode(result) != payload:
                raise ValueError
            return {"result": result, "savedAt": saved_at, "contentHash": digest}
        except (ValueError, TypeError, RecursionError, OverflowError):
            raise UsageArchiveError(_CORRUPT) from None

    @staticmethod
    def _row(connection, thread_id):
        return connection.execute("SELECT payload, content_hash, saved_at, invalidated, reason FROM snapshots WHERE thread_id=?", (thread_id,)).fetchone()

    def load(self, thread_id):
        _identifier(thread_id)
        if not self.path.exists() and not self.path.is_symlink():
            return None
        with self._connection() as connection:
            return self._decode(thread_id, self._row(connection, thread_id))

    def save(self, thread_id, result):
        cleaned = sanitize_result(thread_id, result)
        payload = _encode(cleaned)
        digest = hashlib.sha256(payload).hexdigest()
        with self._connection() as connection:
            row = self._row(connection, thread_id)
            if row is not None and row[3] == 1:
                if cleaned.get("reading", {}).get("complete") is not True:
                    return {"savedAt": row[2], "retained": True, "unchanged": False, "reason": "identity_rejected"}
            elif row is not None:
                previous = self._decode(thread_id, row)
                if previous["contentHash"] == digest:
                    return {"savedAt": previous["savedAt"], "retained": False, "unchanged": True}
                reason = _regression(previous["result"], cleaned)
                if reason:
                    return {"savedAt": previous["savedAt"], "retained": True, "unchanged": False, "reason": reason}
            saved_at = time.time()
            connection.execute("INSERT INTO snapshots(thread_id,payload,content_hash,saved_at,invalidated,reason) VALUES(?,?,?,?,0,NULL) ON CONFLICT(thread_id) DO UPDATE SET payload=excluded.payload,content_hash=excluded.content_hash,saved_at=excluded.saved_at,invalidated=0,reason=NULL", (thread_id, payload, digest, saved_at))
            return {"savedAt": saved_at, "retained": False, "unchanged": False}

    def invalidate(self, thread_id, reason="identity_rejected"):
        _identifier(thread_id)
        # Store only a stable code, not a caller-provided exception or log text.
        reason = "identity_rejected" if reason != "manual_invalidation" else reason
        with self._connection() as connection:
            connection.execute("INSERT INTO snapshots(thread_id,payload,content_hash,saved_at,invalidated,reason) VALUES(?,NULL,NULL,?,1,?) ON CONFLICT(thread_id) DO UPDATE SET payload=NULL,content_hash=NULL,saved_at=excluded.saved_at,invalidated=1,reason=excluded.reason", (thread_id, time.time(), reason))

    def stats(self):
        if not self.path.exists() and not self.path.is_symlink():
            return {"savedThreads": 0, "invalidatedThreads": 0}
        with self._connection() as connection:
            rows = dict(connection.execute("SELECT invalidated, count(*) FROM snapshots GROUP BY invalidated").fetchall())
            return {"savedThreads": rows.get(0, 0), "invalidatedThreads": rows.get(1, 0)}
