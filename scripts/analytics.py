"""Bounded local-log analytics, with no account or execution-device inference.

Uses the existing API estimator for every validated model/context slice. Nothing
is read, registered or saved here; the caller supplies its full observed ledger.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP, localcontext

from periods import _prepare, _anchor, _adjacent_month, select_subscription_turns
from pricing import (API_RATE_DATE, API_SOURCE_URL, API_RATES, TOKEN_FIELDS, safe_model,
                     validated_slices, _estimate_single)


_API_SETTINGS = {"pricingMode": "api", "currencyPerUsd": "1"}
_COMPONENTS = ("uncachedInput", "cachedInput", "output")
_MICRO = Decimal("0.000001")
MAX_MODEL_ROWS = 24
MAX_TOP_CONVERSATIONS = 8


def _zero():
    return dict.fromkeys(TOKEN_FIELDS, 0)


def _new_group(model):
    rate = API_RATES.get(model)
    return {"model": model, "label": rate[0] if rate else model or "未确认模型",
            "tokens": _zero(), "money": Decimal(0), "available": False,
            "parts": dict.fromkeys(_COMPONENTS, Decimal(0)), "partial": False,
            "unpricedTokens": 0, "apiContextUncertainTokens": 0,
            "apiLongContextTokens": 0, "turnCount": 0}


def _new_bucket(partial=False):
    return {"groups": {}, "partial": partial, "turnCount": 0, "observed": False}


def _add_fragment(bucket, fragment, price, partial):
    bucket["observed"] = True
    group = bucket["groups"].get(fragment["model"])
    if group is None:
        group = bucket["groups"][fragment["model"]] = _new_group(fragment["model"])
    for key in TOKEN_FIELDS:
        group["tokens"][key] += fragment["tokens"][key]
    group["partial"] |= partial or price["status"] != "estimated"
    group["apiContextUncertainTokens"] += price.get("apiContextUncertainTokens", 0)
    group["apiLongContextTokens"] += price.get("apiLongContextTokens", 0)
    if price["usd"] is None:
        group["unpricedTokens"] += fragment["tokens"]["total"]
    else:
        group["available"] = True
        group["money"] += Decimal(price["usd"])
        for key in _COMPONENTS:
            group["parts"][key] += Decimal(price["breakdown"][key]["usd"])


def _quantize(value):
    return value.quantize(_MICRO, rounding=ROUND_HALF_UP)


def _rounded_parts(parts, total):
    """Allocate only sub-micro rounding to keep displayed components conserved."""
    result = {key: _quantize(value) for key, value in parts.items()}
    remainder = total - sum(result.values())
    if remainder:
        # The largest component can absorb a micro-unit without becoming
        # negative. Never manufacture a cost for an unpriced token category.
        largest = max(parts, key=parts.get)
        result[largest] += remainder
    return result


def _merge_group(target, source):
    for key in TOKEN_FIELDS:
        target["tokens"][key] += source["tokens"][key]
    for key in _COMPONENTS:
        target["parts"][key] += source["parts"][key]
    target["money"] += source["money"]
    target["available"] |= source["available"]
    target["partial"] |= source["partial"]
    for key in ("unpricedTokens", "apiContextUncertainTokens", "apiLongContextTokens", "turnCount"):
        target[key] += source[key]


def _commit_turn(target, source):
    if not source["observed"]:
        return
    target["observed"] = True
    target["turnCount"] += 1
    for model, group in source["groups"].items():
        # Match pricing._estimate_turn: round once per model within each turn,
        # then add the model/turn amounts. Daily views round at their own scope.
        group["money"] = _quantize(group["money"])
        group["parts"] = _rounded_parts(group["parts"], group["money"])
        group["turnCount"] = 1
        _merge_group(target["groups"].setdefault(model, _new_group(model)), group)
        target["partial"] |= group["partial"]


def _cache_rate(tokens):
    return tokens["cachedInput"] / tokens["input"] if tokens and tokens["input"] else None


def _model_row(group):
    return {key: group[key] for key in ("model", "label", "tokens", "partial", "unpricedTokens",
                                      "apiContextUncertainTokens", "apiLongContextTokens", "turnCount")} | {
        "usd": format(group["money"], "f") if group["available"] else None}


def _finish(bucket, unassigned, *, models=True):
    groups = list(bucket["groups"].values())
    tokens = {key: sum(group["tokens"][key] for group in groups) for key in TOKEN_FIELDS}
    partial = bucket["partial"] or bool(unassigned)
    available = any(group["available"] for group in groups)
    empty = not bucket["observed"] and not partial
    money = sum((group["money"] for group in groups), Decimal(0))
    value = {
        "tokens": tokens if bucket["observed"] or not partial else None,
        "usd": format(_quantize(money), "f") if available or empty else None,
        "partial": partial, "turnCount": bucket["turnCount"],
        "unpricedTokens": sum(group["unpricedTokens"] for group in groups),
        "apiContextUncertainTokens": sum(group["apiContextUncertainTokens"] for group in groups),
        "apiLongContextTokens": sum(group["apiLongContextTokens"] for group in groups),
        "unassignedTokens": unassigned,
        "cacheHitRate": _cache_rate(tokens),
        "costBreakdown": {key + "Usd": format(_quantize(sum(
            (group["parts"][key] for group in groups), Decimal(0))), "f") if available or empty else None
            for key in _COMPONENTS},
    }
    value["status"] = "unavailable" if value["usd"] is None else "partial" if partial else "complete"
    if models:
        ordered = sorted(groups, key=lambda group: (-group["tokens"]["total"], group["model"] or ""))
        # Keep the unknown-model row visible even if arbitrary observed model
        # identifiers create more groups than the chart can reasonably display.
        known = [group for group in ordered if group["model"] is not None]
        unknown = [group for group in ordered if group["model"] is None]
        keep_count = MAX_MODEL_ROWS - len(unknown) - (len(ordered) > MAX_MODEL_ROWS)
        shown, rest = known[:keep_count] + unknown, known[keep_count:]
        value["models"] = [_model_row(group) for group in shown]
        if rest:
            other = _new_group(None)
            other["label"] = "其他模型"
            for group in rest:
                _merge_group(other, group)
            value["models"].append(_model_row(other) | {"model": "__other__", "groupedOther": True})
    return value


def _build_analytics(turns, settings, *, now=None, reading_incomplete=False, read_error_count=0,
                     period_override=None):
    """Summarize full locally registered history, never the UI's recent-row cap.

    Cached input is a subset of input; reasoning is a subset of output. Missing
    dates remain unassigned. Account quota never participates in this function.
    """
    if now is None:
        now = datetime.now().astimezone()
    if isinstance(now, datetime):
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must have a timezone")
        today = now.date()
        generated_at = now.isoformat()
    elif isinstance(now, date):
        today, generated_at = now, None
    else:
        raise ValueError("now must be a date or aware datetime")
    settings = settings if isinstance(settings, dict) else {}
    invalid = not isinstance(turns, (list, tuple))
    prepared, unassigned, gap = _prepare([] if invalid else turns)
    gap |= bool(invalid or reading_incomplete or read_error_count)
    gap |= bool(unassigned)
    tomorrow = today + timedelta(days=1)
    ranges = {"today": (today, tomorrow), "week": (today - timedelta(days=6), tomorrow),
              "month": (today - timedelta(days=29), tomorrow)}
    renewal = settings.get("subscriptionRenewalDay")
    if type(renewal) is int and 1 <= renewal <= 31:
        start = _anchor(today.year, today.month, renewal)
        if start > today:
            start = _adjacent_month(start, -1, renewal)
        ranges["subscription"] = (start, _adjacent_month(start, 1, renewal))
    if period_override is not None:
        ranges = {"subscription": (date.fromisoformat(period_override["startDate"]),
                                   date.fromisoformat(period_override["endDate"]))}
        unassigned += period_override["unassignedTokens"]
        gap |= period_override["coverageGap"] or bool(unassigned)
    buckets = {name: _new_bucket(gap) for name in ranges}
    conversations = {name: {} for name in ranges}
    earliest = min(start for start, _ in ranges.values())
    chart_end = ranges["subscription"][1] if period_override is not None else tomorrow
    days = {}
    cursor = earliest
    while cursor < chart_end:
        days[cursor.isoformat()] = _new_bucket(gap)
        cursor += timedelta(days=1)
    with localcontext() as context:
        context.prec = 100
        for turn, daily in prepared:
            try:
                fragments = validated_slices(turn)
                invalid_slices = False
            except (ValueError, TypeError, OverflowError):
                fragments, invalid_slices = None, True
            if fragments is None:
                # Legacy day buckets preserve a known model; a malformed slice
                # set invalidates its model evidence without losing its counters.
                fragments = [{"day": day.isoformat(), "tokens": tokens,
                              "model": None if invalid_slices else safe_model(turn.get("model")),
                              "serviceTier": None if invalid_slices else turn.get("serviceTier"),
                              "pricingMetadataStatus": "unknown" if invalid_slices else turn.get("pricingMetadataStatus"),
                              "apiContext": turn.get("apiContext")}
                             for day, tokens in daily.items()]
            local_scopes = {name: _new_bucket() for name in ranges}
            local_days = {}
            partial = turn.get("quality") != "complete" or bool(turn.get("readingIncomplete")) or invalid_slices
            for fragment in fragments:
                day_text = fragment["day"]
                if day_text not in days:
                    continue
                day = date.fromisoformat(day_text)
                price = _estimate_single({**fragment, "quality": turn.get("quality"),
                                          "status": turn.get("status")}, _API_SETTINGS, _unrounded=True)
                _add_fragment(local_days.setdefault(day_text, _new_bucket()), fragment, price, partial)
                for name, (start, end) in ranges.items():
                    if start <= day < end:
                        _add_fragment(local_scopes[name], fragment, price, partial)
            for name, bucket in local_scopes.items():
                _commit_turn(buckets[name], bucket)
                thread_id = safe_model(turn.get("threadId"))
                if bucket["observed"] and thread_id is not None:
                    target = conversations[name].get(thread_id)
                    if target is None:
                        target = conversations[name][thread_id] = _new_bucket(gap)
                    # Reuse the just-rounded per-turn model amounts so this
                    # ranking and the period total have identical accounting.
                    _commit_turn(target, bucket)
            for day, bucket in local_days.items():
                _commit_turn(days[day], bucket)
        finished_days = {day: {"date": day, **_finish(bucket, unassigned)}
                         for day, bucket in days.items()}
        periods = {}
        for name, bucket in buckets.items():
            start, end = ranges[name]
            ranked = []
            for thread_id, thread_bucket in conversations[name].items():
                value = _finish(thread_bucket, unassigned, models=False)
                ranked.append({"threadId": thread_id, **{key: value[key] for key in
                    ("tokens", "usd", "partial", "unpricedTokens", "turnCount")}})
            ranked.sort(key=lambda row: (row["usd"] is None,
                        -Decimal(row["usd"]) if row["usd"] is not None else Decimal(0),
                        -row["tokens"]["total"], row["threadId"]))
            periods[name] = {"startDate": start.isoformat(), "endDate": end.isoformat(),
                             **_finish(bucket, unassigned),
                             "topConversations": ranked[:MAX_TOP_CONVERSATIONS],
                             "topConversationsCount": len(ranked),
                             "daily": [value for day, value in finished_days.items()
                                       if start.isoformat() <= day < end.isoformat()]}
    if "subscription" not in periods:
        periods["subscription"] = {**_finish(_new_bucket(True), unassigned), "status": "needs_configuration",
                                   "startDate": None, "endDate": None, "daily": [],
                                   "topConversations": [], "topConversationsCount": 0}
    return {"version": 1, "scope": "local_registered_logs", "billingBasis": "api_standard",
            "today": today.isoformat(), "generatedAt": generated_at, "rateDate": API_RATE_DATE,
            "sourceUrl": API_SOURCE_URL, "unassignedTokens": unassigned, "periods": periods,
            "note": "仅分析本机已登记并采集的日志；同步副本不证明执行设备或付款账号。账号官方额度可能同时由其他设备消耗，差额本身不代表本机采集缺失。",
            "roundingNote": "按每轮、每个模型保留 6 位美元小数；跨日图表分别舍入，逐日金额之和可能有微小尾差。"}


def build_analytics(turns, settings, *, now=None, reading_incomplete=False, read_error_count=0,
                    subscription_selection=None):
    """Keep daily views intact while clipping the custom period by event time."""
    if now is None:
        now = datetime.now().astimezone()
    options = dict(now=now, reading_incomplete=reading_incomplete, read_error_count=read_error_count)
    result = _build_analytics(turns, settings, **options)
    selected = subscription_selection
    if selected is None:
        selected = select_subscription_turns(turns, settings)
    if selected is not None:
        custom = _build_analytics(selected["turns"], settings, period_override=selected, **options)
        period = custom["periods"]["subscription"]
        period.update({key: selected[key] for key in ("source", "startAt", "endAt", "timeZone", "endExclusive")})
        result["periods"]["subscription"] = period
    return result
