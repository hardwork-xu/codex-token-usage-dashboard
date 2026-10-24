"""Frozen classification of observed usage by local-log verifiability.

This is a manual residual allocation, not evidence of execution hardware or a
paying account. It neither discovers logs nor adds usage to the source ledger.
Only aggregate counters and API Standard USD subtotals leave this module.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path
import re

from periods import summarize_periods
from pricing import estimate_turn


SOURCE = "manual_residual_from_local_log_coverage"
MAX_ALLOCATION_BYTES = 16 * 1024
_FIELDS = ("total", "input", "cachedInput", "cacheWriteInput", "output", "reasoningOutput")
_GROUPS = ("total", "local", "other")
_PERIODS = ("today", "subscription", "allTime")
_LIMIT = 10**40
_USD = re.compile(r"(?:0|[1-9][0-9]{0,39})\.[0-9]{6}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


class DeviceAllocationError(ValueError):
    """Content-free codes safe to display without leaking source material."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code="allocation_invalid"):
    raise DeviceAllocationError(code)


def _integer(value):
    return type(value) is int and 0 <= value <= _LIMIT


def _tokens(value):
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != set(_FIELDS)
            or not all(_integer(value[key]) for key in _FIELDS)
            or value["total"] != value["input"] + value["output"]
            or value["cachedInput"] > value["input"]
            or value["reasoningOutput"] > value["output"]):
        _fail("allocation_invalid")
    return dict(value)


def _api_settings(settings):
    if not isinstance(settings, dict):
        _fail("allocation_invalid")
    renewal = settings.get("subscriptionRenewalDay")
    if type(renewal) is not int or not 1 <= renewal <= 31:
        renewal = None
    return {"pricingMode": "api", "currencyPerUsd": "1", "speedMode": "standard",
            "subscriptionRenewalDay": renewal}


def pricing_fingerprint(settings):
    """Bind the frozen USD method and period settings, never the display FX."""
    digest = hashlib.sha256(json.dumps(_api_settings(settings), sort_keys=True,
                                       separators=(",", ":")).encode("ascii"))
    for filename in ("pricing.py", "periods.py"):
        digest.update((Path(__file__).parent / filename).read_bytes())
    return digest.hexdigest()


def _conversation_state(row):
    if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
        _fail()
    reading, archive = row.get("reading"), row.get("archive") or {}
    if not isinstance(archive, dict) or reading is not None and not isinstance(reading, dict):
        _fail()
    acknowledged_missing = (archive.get("historicalGap") is True
                            and archive.get("reason") == "source_unavailable"
                            and row.get("readErrorCode") == "source_missing")
    if ((reading is not None and reading.get("complete") is not True)
            or row.get("readError") and not acknowledged_missing
            or row.get("readErrorCode") and not acknowledged_missing
            or archive.get("reason") in {"backfilling", "source_changed"}
            or archive.get("writeError") or archive.get("writeErrors")
            or archive.get("reason") == "source_unavailable" and not acknowledged_missing
            or reading is None and not acknowledged_missing):
        _fail("collection_not_ready")
    continuation = archive.get("continuation") or {}
    if not isinstance(continuation, dict):
        _fail()
    local = (reading is not None and reading.get("complete") is True
             and archive.get("source") in {"live", "compressed"}
             and not archive.get("reason")
             and continuation.get("status") != "continuation_with_gap")
    return local, acknowledged_missing


def _all_time(turns, settings, *, unknown_tasks, unassigned):
    total = dict.fromkeys(_FIELDS, 0)
    usd, has_tokens, has_usd = Decimal(0), False, False
    unknown_turns = unpriced = 0
    partial = bool(unknown_tasks)
    for turn in turns:
        tokens = _tokens(turn.get("tokens"))
        if tokens is None:
            unknown_turns += 1
            partial = True
            continue
        has_tokens = True
        for field in _FIELDS:
            total[field] += tokens[field]
        estimate = estimate_turn(turn, settings)
        if estimate["usd"] is not None:
            usd += Decimal(estimate["usd"])
            has_usd = True
        unpriced += estimate.get("unpricedTokens", tokens["total"] if estimate["usd"] is None else 0)
        partial |= (turn.get("quality") != "complete" or bool(turn.get("readingIncomplete"))
                    or estimate["status"] != "estimated")
    empty_known = not turns and not unknown_tasks
    return {"tokens": total if has_tokens or empty_known else None,
            "usd": format(usd, ".6f") if has_usd or empty_known else None,
            "unpricedTokens": unpriced, "unassignedTokens": unassigned,
            "unknownTurnCount": unknown_turns, "partial": partial}


def _period_row(period, unknown_turns):
    return {"tokens": deepcopy(period["tokens"]), "usd": period["usd"],
            "unpricedTokens": period["unpricedTokens"],
            "unassignedTokens": period["unassignedTokens"],
            "unknownTurnCount": unknown_turns, "partial": period["partial"]}


def build_allocation(turns, conversations, settings, now):
    """Classify a complete supplied ledger once; never scan or persist sources.

    The caller must additionally reject global archive/collection write errors;
    those service-level errors are not present in this function's parameters.
    A missing log is admissible only when its source_missing error was explicitly
    acknowledged. Unknown usage stays unknown, including empty missing tasks.
    """
    if (not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None
            or now.date() == date.max
            or not isinstance(turns, (list, tuple)) or not isinstance(conversations, (list, tuple))
            or len(turns) > 1_000_000 or len(conversations) > 100_000):
        _fail()
    options = _api_settings(settings)
    catalogue = {}
    for conversation in conversations:
        local, missing = _conversation_state(conversation)
        thread = conversation["id"]
        if thread in catalogue:
            _fail()
        catalogue[thread] = (local, missing)
    # Match summarize_periods' last-observation-wins rule before partitioning.
    # Otherwise two snapshots of one turn could leak into different groups.
    unique = {}
    for turn in turns:
        if (not isinstance(turn, dict) or not isinstance(turn.get("threadId"), str)
                or not isinstance(turn.get("id"), str) or not turn["id"]
                or turn["threadId"] not in catalogue):
            _fail()
        _tokens(turn.get("tokens"))
        unique[(turn["threadId"], turn["id"])] = turn
    grouped = {"total": list(unique.values()), "local": [], "other": []}
    known_tasks, seen_tasks = set(), set()
    for turn in grouped["total"]:
        thread = turn["threadId"]
        seen_tasks.add(thread)
        if turn.get("tokens") is not None:
            known_tasks.add(thread)
        group = "local" if catalogue[thread][0] and turn.get("sourceAvailability") == "live" else "other"
        grouped[group].append(turn)
    unknown_tasks = {thread for thread, (_, missing) in catalogue.items()
                     if thread not in known_tasks and (missing or thread in seen_tasks)}
    local_threads = {turn["threadId"] for turn in grouped["local"]}
    unknown_by_group = {"total": len(unknown_tasks), "local": len(unknown_tasks & local_threads)}
    unknown_by_group["other"] = unknown_by_group["total"] - unknown_by_group["local"]
    periods = {key: {} for key in _PERIODS}
    with localcontext() as context:
        context.prec = 100
        for group, values in grouped.items():
            summary = summarize_periods(values, options, now=now,
                                        read_error_count=unknown_by_group[group])
            unknown_turns = sum(turn.get("tokens") is None for turn in values)
            for key in ("today", "subscription"):
                item = summary[key]
                periods[key].update(startDate=item["startDate"], endDate=item["endDate"])
                periods[key][group] = _period_row(item, unknown_turns)
            periods["allTime"].update(startDate=None, endDate=None)
            periods["allTime"][group] = _all_time(values, options, unknown_tasks=unknown_by_group[group],
                                                  unassigned=summary["unassignedTokens"])
    result = {"schemaVersion": 1, "capturedAt": now.isoformat(timespec="microseconds"),
              "localDate": now.date().isoformat(), "source": SOURCE,
              "pricingFingerprint": pricing_fingerprint(settings),
              "unknownTaskCount": len(unknown_tasks), "periods": periods}
    return validate_allocation(result)


def _date(value):
    if not isinstance(value, str):
        _fail()
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        _fail()
    if parsed.isoformat() != value:
        _fail()
    return parsed


def _conserve(period):
    for field in _FIELDS:
        def count(group):
            value = period[group]["tokens"]
            return 0 if value is None else value[field]
        if count("total") != count("local") + count("other"):
            _fail("conservation_failed")
    for field in ("unpricedTokens", "unassignedTokens", "unknownTurnCount"):
        if period["total"][field] != period["local"][field] + period["other"][field]:
            _fail("conservation_failed")
    money = lambda group: Decimal(period[group]["usd"]) if period[group]["usd"] is not None else Decimal(0)
    if money("total") != money("local") + money("other"):
        _fail("conservation_failed")
    if period["total"]["usd"] is None and any(period[group]["usd"] not in (None, "0.000000") for group in ("local", "other")):
        _fail("conservation_failed")


def validate_allocation(value):
    """Validate a bounded, closed aggregate schema; return an isolated copy."""
    required = {"schemaVersion", "capturedAt", "localDate", "source", "pricingFingerprint", "unknownTaskCount", "periods"}
    if (not isinstance(value, dict) or set(value) != required
            or type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1
            or value["source"] != SOURCE or not isinstance(value["pricingFingerprint"], str)
            or not _HASH.fullmatch(value["pricingFingerprint"]) or not _integer(value["unknownTaskCount"])
            or not isinstance(value["periods"], dict) or set(value["periods"]) != set(_PERIODS)
            or not isinstance(value["capturedAt"], str) or len(value["capturedAt"]) > 40):
        _fail()
    try:
        captured = datetime.fromisoformat(value["capturedAt"])
    except ValueError:
        _fail()
    today = _date(value["localDate"])
    if (captured.tzinfo is None or captured.utcoffset() is None or captured.date() != today
            or today == date.max or captured.isoformat(timespec="microseconds") != value["capturedAt"]):
        _fail()
    with localcontext() as context:
        context.prec = 100
        for key, period in value["periods"].items():
            if not isinstance(period, dict) or set(period) != {"startDate", "endDate", *_GROUPS}:
                _fail()
            start, end = period["startDate"], period["endDate"]
            if key == "allTime":
                if start is not None or end is not None:
                    _fail()
            elif start is None and end is None and key == "subscription":
                pass
            elif _date(start) >= _date(end):
                _fail()
            if key == "today" and (start != today.isoformat() or end != (today + timedelta(days=1)).isoformat()):
                _fail()
            for group in _GROUPS:
                row = period[group]
                if (not isinstance(row, dict) or set(row) != {"tokens", "usd", "unpricedTokens", "unassignedTokens", "unknownTurnCount", "partial"}
                        or type(row["partial"]) is not bool
                        or not all(_integer(row[field]) for field in ("unpricedTokens", "unassignedTokens", "unknownTurnCount"))):
                    _fail()
                _tokens(row["tokens"])
                if row["usd"] is not None and (not isinstance(row["usd"], str) or not _USD.fullmatch(row["usd"])):
                    _fail()
                if row["tokens"] is None and row["usd"] is not None:
                    _fail()
                needs_configuration = key == "subscription" and start is None
                if (not row["partial"] and (row["unpricedTokens"] or row["unknownTurnCount"]
                        or key != "allTime" and row["unassignedTokens"]
                        or not needs_configuration and (row["tokens"] is None or row["usd"] is None))):
                    _fail()
                if row["tokens"] is None and row["unpricedTokens"]:
                    _fail()
                if row["tokens"] is not None and row["unpricedTokens"] > row["tokens"]["total"]:
                    _fail()
                if (row["tokens"] is not None and row["tokens"]["total"] > 0
                        and row["unpricedTokens"] == row["tokens"]["total"] and row["usd"] is not None):
                    _fail()
                if key == "subscription" and start is None and (row["tokens"] is not None or row["usd"] is not None):
                    _fail()
            _conserve(period)
        lifetime = value["periods"]["allTime"]
        if value["unknownTaskCount"] and not lifetime["total"]["partial"]:
            _fail()
        for group in _GROUPS:
            all_tokens = lifetime[group]["tokens"]
            if (all_tokens is not None and lifetime[group]["unassignedTokens"] > all_tokens["total"]):
                _fail()
            for key in ("today", "subscription"):
                row = value["periods"][key][group]
                if (row["unknownTurnCount"] != lifetime[group]["unknownTurnCount"]
                        or row["unassignedTokens"] != lifetime[group]["unassignedTokens"]):
                    _fail()
                if row["tokens"] is not None and any(
                        row["tokens"][field] > (all_tokens or {}).get(field, 0) for field in _FIELDS):
                    _fail()
    try:
        encoded = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError, RecursionError):
        _fail()
    if len(encoded) > MAX_ALLOCATION_BYTES:
        _fail()
    return deepcopy(value)


__all__ = ["build_allocation", "validate_allocation", "pricing_fingerprint", "DeviceAllocationError", "MAX_ALLOCATION_BYTES"]
