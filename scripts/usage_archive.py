"""Private, durable snapshots of already verified, normalized usage metadata.

This is not a log discovery mechanism or a billing ledger. Only a strict numeric
and identity schema is retained; messages, paths, account data and price totals
are never copied. A snapshot replaces another snapshot atomically. Conflicting
versions are never added together or spliced into a fabricated history.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
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


def _ordinal(value):
    if type(value) is not int or not 0 <= value < 2**64:
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
        if "responseId" in value or "responseAccounting" in value:
            result["responseId"] = _identifier(value.get("responseId"))
            result["responseAccounting"] = _enum(value.get("responseAccounting"), ("paired", "compaction"))
            if "responseOrdinal" in value:
                result["responseOrdinal"] = _ordinal(value["responseOrdinal"])
        if "eventOrdinal" in value or "eventPart" in value:
            result["eventOrdinal"] = _ordinal(value.get("eventOrdinal"))
            part = value.get("eventPart")
            if type(part) is not int or part not in (0, 1):
                _invalid()
            result["eventPart"] = part
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
    for key in ("startOrdinal", "endOrdinal"):
        if key in value:
            result[key] = _ordinal(value[key])
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
    response_ids = [row["responseId"] for turn in result["turns"] for row in turn.get("usageSegments", [])
                    if "responseId" in row]
    if len(set(response_ids)) != len(response_ids):
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
    if "sourceHistory" in value:
        history = value["sourceHistory"]
        if not isinstance(history, dict):
            _invalid()
        result["sourceHistory"] = {
            "mode": _enum(history.get("mode"), ("paginated",)),
            "firstOrdinal": _ordinal(history.get("firstOrdinal")),
            "lastOrdinal": _ordinal(history.get("lastOrdinal")),
            "eligible": _bool(history.get("eligible")),
            "sawRollback": _bool(history.get("sawRollback")),
        }
        if result["sourceHistory"]["lastOrdinal"] < result["sourceHistory"]["firstOrdinal"]:
            _invalid()
    if "continuation" in value:
        continuation = value["continuation"]
        if not isinstance(continuation, dict):
            _invalid()
        result["continuation"] = {
            "status": _enum(continuation.get("status"), ("overlap_verified", "continuation_with_gap")),
            "retainedTurns": _integer(continuation.get("retainedTurns")),
            "sourceTurns": _integer(continuation.get("sourceTurns")),
            "overlapSegments": _integer(continuation.get("overlapSegments")),
            "legacyAnchor": _bool(continuation.get("legacyAnchor")),
            "sourceFirstOrdinal": _ordinal(continuation.get("sourceFirstOrdinal")),
            "sourceLastOrdinal": _ordinal(continuation.get("sourceLastOrdinal")),
        }
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


def _segment_value(segment):
    """Exact numeric/time/context evidence, independent of newer provenance."""
    return {key: value for key, value in segment.items()
            if key not in {"eventOrdinal", "eventPart", "responseId", "responseAccounting", "responseOrdinal"}}


def _verified_response_addition(row, old, seen):
    """Only parser-verified, independent compaction receipts can fill old holes."""
    response_id = row.get("responseId")
    if (row.get("responseAccounting") != "compaction" or response_id is None or response_id in seen
            or row.get("responseOrdinal") != row.get("eventOrdinal") or row.get("eventPart") != 0
            or row.get("timestamp") is None):
        return False
    if old.get("startedAt") is not None and row["timestamp"] < old["startedAt"]:
        return False
    if old.get("endedAt") is not None and row["timestamp"] > old["endedAt"]:
        return False
    return True


def _carry_response_evidence(previous, current):
    """A projected page may omit receipts, but cannot erase their saved proof."""
    fields = ("responseId", "responseAccounting", "responseOrdinal")
    newer = {turn["id"]: turn for turn in current["turns"]}
    for old in previous["turns"]:
        if old["id"] not in newer:
            continue
        observed = [row for row in old.get("usageSegments", []) if "responseId" in row]
        if not observed:
            continue
        after = newer[old["id"]].get("usageSegments", [])
        ordinals = {(row["eventOrdinal"], row["eventPart"]): row for row in after if "eventOrdinal" in row}
        numeric = {}
        for row in after:
            key = _encode(_segment_value(row))
            numeric[key] = None if key in numeric else row
        for row in observed:
            matched = (ordinals.get((row["eventOrdinal"], row["eventPart"])) if "eventOrdinal" in row
                       else numeric.get(_encode(_segment_value(row))))
            if matched is None:
                continue
            if _segment_value(row) != _segment_value(matched):
                return False
            for key in fields:
                if key in row and key in matched and row[key] != matched[key]:
                    return False
                if key in row:
                    matched[key] = row[key]
    return True


def _last_observation(turn):
    times = [turn.get("startedAt"), turn.get("endedAt")]
    times.extend(row.get("timestamp") for row in turn.get("usageSegments", []))
    return max((value for value in times if value is not None), default=None)


def _full_source_preserves_ledger(previous, current):
    """A complete zero-based source may replace a union only with exact evidence.

    Totals alone cannot prove that a restored full source contains the same
    historical model/speed observations. Require every retained event to occur
    unchanged; a complete source may additionally fill previously unseen gaps.
    """
    history = current.get("sourceHistory", {})
    if (history.get("mode") != "paginated" or history.get("firstOrdinal") != 0
            or not history.get("eligible") or history.get("sawRollback")
            or current.get("reading", {}).get("complete") is not True):
        return False
    for key in ("sourceType", "parentThreadId"):
        if previous.get("conversationMetadata", {}).get(key) != current.get("conversationMetadata", {}).get(key):
            return False
    newer = {turn["id"]: turn for turn in current["turns"]}
    for old in previous["turns"]:
        new = newer.get(old["id"])
        if new is None:
            return False
        for key in ("startedAt", "endedAt", "startOrdinal", "endOrdinal"):
            if old.get(key) is not None and old[key] != new.get(key):
                return False
        before, after = old.get("usageSegments"), new.get("usageSegments")
        if not isinstance(before, list) or not isinstance(after, list):
            if any(old.get(key) != new.get(key) for key in ("tokens", "usageSlices", "dailyUsage", "undatedTokens")):
                return False
            continue
        if all("eventOrdinal" in row for row in before + after):
            positions = {(row["eventOrdinal"], row["eventPart"]): row for row in after}
            if len(positions) != len(after):
                return False
            if any((matched := positions.get((row["eventOrdinal"], row["eventPart"]))) is None
                   or _segment_value(row) != _segment_value(matched) for row in before):
                return False
        else:
            # Old-schema events have no ordinal. Each exact timestamp/numeric
            # observation must have one unambiguous, correctly ordered match.
            positions = {}
            for index, candidate in enumerate(after):
                key = _encode(_segment_value(candidate))
                positions[key] = -1 if key in positions else index
            position = -1
            for row in before:
                matched = positions.get(_encode(_segment_value(row)), -1)
                if row.get("timestamp") is None or matched <= position:
                    return False
                position = matched
    return True


def _join_turn(old, new):
    """Return (turn, overlap count, legacy anchor, added segments), or None.

    Exact ordinal/part matches or a unique contiguous legacy numeric overlap
    permit an extension. A repeated timestamp/amount alone is never a match.
    """
    for key in ("startedAt", "endedAt", "startOrdinal", "endOrdinal"):
        if old.get(key) is not None and new.get(key) is not None and old[key] != new[key]:
            return None
    before, after = old.get("usageSegments"), new.get("usageSegments")
    if not isinstance(before, list) or not isinstance(after, list):
        # Legacy aggregate-only snapshots can be retained, but not extended
        # without event observations. A finished exact turn is a bounded anchor.
        keys = ("tokens", "startedAt", "endedAt", "usageSlices")
        if old.get("endedAt") is not None and all(old.get(key) == new.get(key) for key in keys):
            return deepcopy(old), 1, True, 0
        return None
    overlap = 0
    legacy = False
    merged = deepcopy(before)
    responses = {row["responseId"] for row in before if "responseId" in row}
    inserted_receipts = 0
    all_ordinals = all("eventOrdinal" in row for row in before + after)
    if all_ordinals:
        positions = {(row["eventOrdinal"], row["eventPart"]): index for index, row in enumerate(before)}
        if len(positions) != len(before):
            return None
        high = max((row["eventOrdinal"] for row in before), default=-1)
        seen = set()
        for row in after:
            key = (row["eventOrdinal"], row["eventPart"])
            if key in seen:
                return None
            seen.add(key)
            if key in positions:
                original = before[positions[key]]
                if (original.get("responseId") is not None and row.get("responseId") is not None
                        and original["responseId"] != row["responseId"]):
                    return None
                if _segment_value(before[positions[key]]) != _segment_value(row):
                    return None
                # Add request provenance without changing the prior observation.
                merged[positions[key]] = deepcopy(row)
                overlap += 1
            elif row["eventOrdinal"] <= high:
                if not _verified_response_addition(row, old, responses):
                    return None
                merged.append(deepcopy(row))
                responses.add(row["responseId"])
                inserted_receipts += 1
            else:
                if row.get("responseId") in responses:
                    return None
                merged.append(deepcopy(row))
                if "responseId" in row:
                    responses.add(row["responseId"])
    else:
        if not after:
            return deepcopy(old), 0, False, 0
        additions = [row for row in after if _verified_response_addition(row, old, responses)]
        after = [row for row in after if row not in additions]
        if not after:
            return None
        first = _segment_value(after[0])
        positions = [index for index, row in enumerate(before) if _segment_value(row) == first]
        # An unknown time or repeated identical observation is not a reliable
        # old-schema migration anchor; do not guess which occurrence it was.
        if after[0].get("timestamp") is None or len(positions) != 1:
            return None
        start = positions[0]
        overlap = min(len(before) - start, len(after))
        if any(_segment_value(before[start + index]) != _segment_value(after[index]) for index in range(overlap)):
            return None
        for index in range(overlap):
            merged[start + index] = deepcopy(after[index])
        merged.extend(deepcopy(after[overlap:]))
        for row in additions:
            if row["responseId"] in responses:
                return None
            responses.add(row["responseId"])
            merged.append(deepcopy(row))
            inserted_receipts += 1
        legacy = True
    added = len(merged) - len(before)
    if added and old.get("endedAt") is not None and old.get("status") == "completed" and added != inserted_receipts:
        return None
    if inserted_receipts:
        merged.sort(key=lambda row: (row.get("timestamp") is None, row.get("timestamp") or 0,
                                     row.get("eventOrdinal", 0), row.get("eventPart", 0)))
    if not added and merged == before:
        result = deepcopy(old)
        # A finish event can arrive after the last usage observation.
        if new.get("endedAt") is not None:
            result.update(endedAt=new["endedAt"], status=new.get("status", old.get("status")))
            if "endOrdinal" in new:
                result["endOrdinal"] = new["endOrdinal"]
        return result, overlap, legacy, 0
    # Rebuild all counters from the conserved event union. Display-day speed
    # declarations are projected by the meter with its current settings.
    from usage_log import _Turn
    rebuilt = _Turn(old["id"], old["threadId"], old.get("startedAt") or new.get("startedAt"))
    for row in merged:
        rebuilt.add(row["tokens"], row.get("timestamp"),
                    metadata={key: row.get(key) for key in ("model", "serviceTier", "pricingMetadataStatus")},
                    api_context=row.get("apiContext"),
                    provenance={key: row[key] for key in ("eventOrdinal", "eventPart", "responseId",
                                                        "responseAccounting", "responseOrdinal") if key in row})
    result = deepcopy(old)
    exported = rebuilt.export()
    for key in ("tokens", "dailyUsage", "undatedTokens", "usageSlices", "usageSegments",
                "model", "serviceTier", "pricingMetadataStatus"):
        result[key] = exported[key]
    if result.get("startedAt") is None:
        result["startedAt"] = new.get("startedAt")
    if new.get("endedAt") is not None:
        result["endedAt"] = new["endedAt"]
    result["status"] = new.get("status", old.get("status")) if added or new.get("endedAt") is not None else old.get("status")
    for key in ("startOrdinal", "endOrdinal"):
        if key not in result and key in new:
            result[key] = new[key]
    if old.get("quality") == "partial" or new.get("quality") == "partial":
        result["quality"] = "partial"
    return result, overlap, legacy, added


def _paginated_continuation(previous, current):
    """Join observed ledger windows, never reconstruct unobserved usage.

    Source history can be paginated or reverted independently of already spent
    usage. Plain truncation, ambiguous overlap and explicit reverts stay retained
    conflicts. A disjoint forward page is admitted only with owned starts and
    strict chronology, and always reports an unverified gap between windows.
    """
    history = current.get("sourceHistory", {})
    if (history.get("mode") != "paginated" or not history.get("eligible")
            or history.get("sawRollback") or history.get("firstOrdinal", 0) <= 0):
        return None
    for key in ("sourceType", "parentThreadId"):
        if previous.get("conversationMetadata", {}).get(key) != current.get("conversationMetadata", {}).get(key):
            return None
    older = {turn["id"]: turn for turn in previous["turns"]}
    newer = {turn["id"]: turn for turn in current["turns"]}
    response_owners = {row["responseId"]: turn["id"] for turn in previous["turns"]
                       for row in turn.get("usageSegments", []) if "responseId" in row}
    if any(row.get("responseId") in response_owners and response_owners[row["responseId"]] != turn["id"]
           for turn in current["turns"] for row in turn.get("usageSegments", [])):
        return None
    latest = max((value for turn in previous["turns"] if (value := _last_observation(turn)) is not None), default=None)
    if latest is None:
        return None
    joined = deepcopy(previous)
    joined_turns = {turn["id"]: turn for turn in joined["turns"]}
    overlap = additions = 0
    legacy = False
    shared = set(older) & set(newer)
    for turn in current["turns"]:
        if turn["id"] in older:
            result = _join_turn(older[turn["id"]], turn)
            if result is None:
                return None
            joined_turns[turn["id"]], matched, migrated, added = result
            overlap += matched
            legacy |= migrated
            additions += added
        else:
            # No event in a new turn may predate the latest archived evidence.
            # The start must belong to this explicit paginated source window.
            if (turn.get("startedAt") is None or turn["startedAt"] <= latest
                    or turn.get("startOrdinal", -1) <= history["firstOrdinal"]
                    or any(row.get("timestamp") is None or row["timestamp"] < turn["startedAt"]
                           for row in turn.get("usageSegments", []))):
                return None
            joined_turns[turn["id"]] = deepcopy(turn)
            additions += len(turn.get("usageSegments", []))
    old_history = previous.get("sourceHistory", {})
    if (current.get("reading", {}).get("complete") is True and old_history.get("eligible")
            and history["firstOrdinal"] == old_history["firstOrdinal"]
            and history["lastOrdinal"] < old_history["lastOrdinal"]):
        return None  # Same projected window was truncated, not advanced.
    if additions and old_history.get("eligible") and history["lastOrdinal"] < old_history["lastOrdinal"]:
        return None
    latest_current = max((value for turn in current["turns"] if (value := _last_observation(turn)) is not None), default=None)
    if (not additions and current.get("reading", {}).get("complete") is True
            and (latest_current is None or latest_current < latest)):
        return None
    disjoint = not shared and bool(newer)
    ordinal_gap = (shared and not overlap and old_history.get("eligible")
                   and history["firstOrdinal"] > old_history["lastOrdinal"])
    if not overlap and not disjoint and not ordinal_gap:
        return None
    if (disjoint or ordinal_gap) and any(
            turn.get("tokens") and any(turn["tokens"].values()) and
            (not turn.get("usageSegments") or any(row.get("timestamp") is None for row in turn["usageSegments"]))
            for turn in previous["turns"]):
        return None
    if ordinal_gap:
        for turn in current["turns"]:
            if any(row.get("timestamp") is None or row["timestamp"] <= latest for row in turn.get("usageSegments", [])):
                return None
    joined["turns"] = list(joined_turns.values())
    for key in ("reading", "sourceHistory", "conversationMetadata"):
        if key in current:
            joined[key] = deepcopy(current[key])
    prior_continuation = previous.get("continuation")
    status = ("continuation_with_gap" if disjoint or ordinal_gap or
              (prior_continuation or {}).get("status") == "continuation_with_gap" else "overlap_verified")
    if not additions and prior_continuation:
        joined["continuation"] = deepcopy(prior_continuation)
    else:
        joined["continuation"] = {"status": status, "retainedTurns": len(set(older) - set(newer)),
                                  "sourceTurns": len(newer), "overlapSegments": overlap, "legacyAnchor": legacy,
                                  "sourceFirstOrdinal": history["firstOrdinal"], "sourceLastOrdinal": history["lastOrdinal"]}
    warning = ("已合并分页前保存的用量与当前页观测；页面之间仍有未核验间隔，不推算缺失用量。"
               if status == "continuation_with_gap" else "已按可核验的事件重叠续接分页用量；保存历史不等于当前源包含完整历史。")
    joined["warnings"] = list(dict.fromkeys(previous.get("warnings", []) + current.get("warnings", []) + [warning]))
    return sanitize_result(current["threadId"], joined)


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
        continuation = None
        with self._connection() as connection:
            row = self._row(connection, thread_id)
            if row is not None and row[3] == 1:
                if cleaned.get("reading", {}).get("complete") is not True:
                    return {"savedAt": row[2], "retained": True, "unchanged": False, "reason": "identity_rejected"}
            elif row is not None:
                previous = self._decode(thread_id, row)
                if previous["contentHash"] == digest:
                    return {"savedAt": previous["savedAt"], "retained": False, "unchanged": True}
                if not _carry_response_evidence(previous["result"], cleaned):
                    return {"savedAt": previous["savedAt"], "retained": True, "unchanged": False,
                            "reason": "pagination_conflict"}
                cleaned = sanitize_result(thread_id, cleaned)
                payload = _encode(cleaned)
                digest = hashlib.sha256(payload).hexdigest()
                if previous["contentHash"] == digest:
                    return {"savedAt": previous["savedAt"], "retained": False, "unchanged": True}
                reason = _regression(previous["result"], cleaned)
                protected = any(turn.get("tokens") and any(turn["tokens"].values()) for turn in previous["result"]["turns"])
                if (protected and previous["result"].get("continuation")
                        and cleaned.get("sourceHistory", {}).get("firstOrdinal", 0) == 0
                        and not _full_source_preserves_ledger(previous["result"], cleaned)):
                    reason = reason or "pagination_conflict"
                if protected and cleaned.get("sourceHistory", {}).get("firstOrdinal", 0) > 0:
                    joined = _paginated_continuation(previous["result"], cleaned)
                    if joined is not None:
                        reason = _regression(previous["result"], joined)
                        if not reason:
                            cleaned = joined
                            continuation = deepcopy(joined["continuation"])
                            payload = _encode(cleaned)
                            digest = hashlib.sha256(payload).hexdigest()
                            if previous["contentHash"] == digest:
                                return {"savedAt": previous["savedAt"], "retained": False, "unchanged": True,
                                        "merged": True, "continuation": continuation}
                    else:
                        reason = reason or "pagination_conflict"
                if reason:
                    return {"savedAt": previous["savedAt"], "retained": True, "unchanged": False, "reason": reason}
            saved_at = time.time()
            connection.execute("INSERT INTO snapshots(thread_id,payload,content_hash,saved_at,invalidated,reason) VALUES(?,?,?,?,0,NULL) ON CONFLICT(thread_id) DO UPDATE SET payload=excluded.payload,content_hash=excluded.content_hash,saved_at=excluded.saved_at,invalidated=0,reason=NULL", (thread_id, payload, digest, saved_at))
            return {"savedAt": saved_at, "retained": False, "unchanged": False,
                    **({"merged": True, "continuation": continuation} if continuation else {})}

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
