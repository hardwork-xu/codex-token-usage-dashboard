"""Pure calendar-period summaries from sanitized, event-dated token counters."""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP, localcontext

from pricing import estimate_turn, validated_slices


_FIELDS = ("total", "input", "cachedInput", "cacheWriteInput", "output", "reasoningOutput")
_MONEY_FIELDS = ("amount", "credits", "usd")


def _zero():
    return dict.fromkeys(_FIELDS, 0)


def _counters(value):
    if not isinstance(value, dict):
        return None
    if any(isinstance(value.get(key), bool) or not isinstance(value.get(key), int)
           or value[key] < 0 for key in _FIELDS):
        return None
    result = {key: value[key] for key in _FIELDS}
    if (result["total"] != result["input"] + result["output"] or
            result["cachedInput"] > result["input"] or
            result["reasoningOutput"] > result["output"]):
        return None
    return result


def _add(target, value):
    for key in _FIELDS:
        target[key] += value[key]


def _day(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _prepare(turns):
    # A turn ID is scoped to its thread. Last occurrence represents its latest
    # snapshot; repeated snapshots must not increase a period's usage.
    unique = {}
    for index, turn in enumerate(turns):
        if not isinstance(turn, dict):
            unique[("invalid", index)] = {}
            continue
        key = ((turn["threadId"], turn["id"]) if
               isinstance(turn.get("threadId"), str) and isinstance(turn.get("id"), str)
               else ("anonymous", index))
        unique[key] = turn
    prepared, unassigned, coverage_gap = [], 0, False
    for turn in unique.values():
        total = _counters(turn.get("tokens"))
        if total is None:
            coverage_gap = True
            continue
        daily, dated_sum = {}, _zero()
        raw_daily = turn.get("dailyUsage")
        if isinstance(raw_daily, dict):
            for label, value in raw_daily.items():
                day, amount = _day(label), _counters(value)
                if day is None or amount is None:
                    coverage_gap = True
                    continue
                daily[day] = amount
                _add(dated_sum, amount)
        residual = {key: total[key] - dated_sum[key] for key in _FIELDS}
        if _counters(residual) is None:
            # Contradictory buckets cannot inflate a period. Keep only the
            # coherent lifetime counters, with no invented date assignment.
            daily, residual = {}, total
            coverage_gap = True
        explicit_undated = turn.get("undatedTokens")
        if explicit_undated is not None and _counters(explicit_undated) != residual:
            coverage_gap = True
        unassigned += residual["total"]
        prepared.append((turn, daily))
    return prepared, unassigned, coverage_gap


def _format(value):
    return format(value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP), "f")


def _period(prepared, settings, start, end, *, unassigned, coverage_gap):
    totals = _zero()
    money = {key: Decimal(0) for key in _MONEY_FIELDS}
    available = dict.fromkeys(_MONEY_FIELDS, False)
    models = {}
    turn_count = unpriced_tokens = unpriced_turns = uncertain_context_tokens = long_context_tokens = 0
    partial = bool(coverage_gap or unassigned)
    for turn, daily in prepared:
        selected = [amount for day, amount in daily.items() if start <= day < end]
        if not selected:
            continue
        sliced = _zero()
        for amount in selected:
            _add(sliced, amount)
        _add(totals, sliced)
        turn_count += 1
        selected_turn = {**turn, "tokens": sliced}
        try:
            slices = validated_slices(turn)
        except (ValueError, TypeError, OverflowError):
            slices = None
            selected_turn.update(model=None, serviceTier=None, pricingMetadataStatus="unknown", quality="partial")
        if slices is not None:
            selected_turn["usageSlices"] = [item for item in slices if item["day"] is not None
                                           and start <= _day(item["day"]) < end]
            selected_turn["dailyUsage"] = {day.isoformat(): amount for day, amount in daily.items() if start <= day < end}
            selected_turn["undatedTokens"] = _zero()
        else:
            selected_turn.pop("usageSlices", None)
        estimate = estimate_turn(selected_turn, settings)
        uncertain_context_tokens += estimate.get("apiContextUncertainTokens", 0)
        long_context_tokens += estimate.get("apiLongContextTokens", 0)
        turn_partial = (turn.get("quality") != "complete" or
                        bool(turn.get("readingIncomplete")) or estimate["status"] == "partial")
        partial |= turn_partial
        unpriced = estimate.get("unpricedTokens", sliced["total"] if estimate["amount"] is None else 0)
        if unpriced:
            unpriced_tokens += unpriced
            unpriced_turns += 1
            partial = True
        for key in _MONEY_FIELDS:
            if estimate[key] is not None:
                money[key] += Decimal(estimate[key])
                available[key] = True
        # One turn may contribute to several model groups. Count that turn once
        # in the period, and once in each participating model (never per slice).
        for row in estimate["models"]:
            model = row["model"]
            group = models.setdefault(model, {
                "model": model, "label": row["label"], "standardRates": row["standardRates"], "tokens": _zero(),
                "unpricedTokens": 0, "apiContextUncertainTokens": 0, "apiLongContextTokens": 0, "turnCount": 0, "partial": bool(coverage_gap or unassigned),
                "money": {key: Decimal(0) for key in _MONEY_FIELDS},
                "available": dict.fromkeys(_MONEY_FIELDS, False),
            })
            _add(group["tokens"], row["tokens"])
            group["turnCount"] += 1
            group["unpricedTokens"] += row["unpricedTokens"]
            group["apiContextUncertainTokens"] += row.get("apiContextUncertainTokens", 0)
            group["apiLongContextTokens"] += row.get("apiLongContextTokens", 0)
            group["partial"] |= turn_partial or row["partial"]
            for key in _MONEY_FIELDS:
                if row[key] is not None:
                    group["money"][key] += Decimal(row[key])
                    group["available"][key] = True

    notes = ["按本机日期汇总已记录用量；金额为已可计价部分的估算，不代表订阅扣款。"]
    if settings.get("pricingMode") == "api":
        notes.append("金额为按各模型当前公开 API Standard 费率重放已记录用量的估算；保留观察到的缓存比例，不含工具等额外费用和税费。")
        if uncertain_context_tokens:
            notes.append("部分请求上下文长度不明，采用该模型短、长上下文价格的中点。")
    if not turn_count:
        notes.append("本时段暂无已记录用量。")
        if not partial:
            available["amount"] = True
            if settings.get("pricingMode", "official") == "official":
                available["credits"] = available["usd"] = True
            elif settings.get("pricingMode") == "api":
                available["usd"] = True
    if coverage_gap:
        notes.append("记录尚未读完、读取失败或计数不完整，汇总可能缺少用量。")
    if unassigned:
        notes.append("部分 Token 无法按日期归属，未计入周期。")
    if unpriced_turns:
        notes.append("部分已记录用量暂无可靠费率，未计入金额。")
    if partial and not coverage_gap and not unassigned and not unpriced_turns:
        notes.append("部分轮次记录不完整，仅汇总已观察到的用量。")
    status = "unavailable" if not available["amount"] else "partial" if partial else "complete"
    return {
        "startDate": start.isoformat(), "endDate": end.isoformat(), "status": status,
        "tokens": totals if turn_count or not partial else None,
        **{key: _format(money[key]) if available[key] else None for key in _MONEY_FIELDS},
        "apiContextUncertainTokens": uncertain_context_tokens, "apiLongContextTokens": long_context_tokens,
        "unpricedTokens": unpriced_tokens, "unpricedTurnCount": unpriced_turns,
        "unassignedTokens": unassigned, "partial": partial, "turnCount": turn_count,
        "models": [
            {key: group[key] for key in ("model", "label", "standardRates", "tokens", "unpricedTokens", "apiContextUncertainTokens", "apiLongContextTokens", "partial", "turnCount")} |
            {key: _format(group["money"][key]) if group["available"][key] else None for key in _MONEY_FIELDS}
            for group in sorted(models.values(), key=lambda group: (
                -group["tokens"]["total"], group["model"] is None, group["model"] or ""))
        ],
        "note": " ".join(notes),
    }


def _anchor(year, month, renewal_day):
    return date(year, month, min(renewal_day, calendar.monthrange(year, month)[1]))


def _adjacent_month(day, direction, renewal_day):
    ordinal = day.year * 12 + day.month - 1 + direction
    year, month = divmod(ordinal, 12)
    return _anchor(year, month + 1, renewal_day)


def summarize_periods(turns, settings, *, now=None, reading_incomplete=False, read_error_count=0):
    """Aggregate all supplied turns; never apply a UI history limit here.

    Default dates use the machine's local calendar. An injected date or aware
    datetime supplies that local calendar date directly, independent of the
    test runner's timezone. Dates with no usable event counters are not inferred
    from turn start/end timestamps. Undated counters are coverage gaps for both
    periods and are never silently allocated to either one.
    """
    if now is None:
        today = datetime.now().astimezone().date()
    elif isinstance(now, datetime):
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Injected datetime must include a timezone")
        today = now.date()
    elif isinstance(now, date):
        today = now
    else:
        raise ValueError("now must be a date or timezone-aware datetime")
    settings = settings if isinstance(settings, dict) else {}
    invalid_turns = not isinstance(turns, (list, tuple))
    prepared, unassigned, gap = _prepare([] if invalid_turns else turns)
    gap |= bool(invalid_turns or reading_incomplete or read_error_count)
    day = settings.get("subscriptionRenewalDay")
    renewal_day = day if isinstance(day, int) and not isinstance(day, bool) and 1 <= day <= 31 else None
    with localcontext() as context:
        context.prec = 100
        today_result = _period(prepared, settings, today, today + timedelta(days=1),
                               unassigned=unassigned, coverage_gap=gap)
        if renewal_day is None:
            subscription = {
                "startDate": None, "endDate": None, "status": "needs_configuration", "tokens": None,
                "amount": None, "credits": None, "usd": None, "unpricedTokens": 0,
                "unpricedTurnCount": 0, "unassignedTokens": unassigned,
                "partial": bool(gap or unassigned), "turnCount": 0,
                "models": [],
                "note": "先设置每月续订日，才能汇总当前订阅周期。",
            }
        else:
            start = _anchor(today.year, today.month, renewal_day)
            if start > today:
                start = _adjacent_month(start, -1, renewal_day)
            end = _adjacent_month(start, 1, renewal_day)
            subscription = _period(prepared, settings, start, end,
                                   unassigned=unassigned, coverage_gap=gap)
    return {"today": today_result, "subscription": subscription,
            "renewalDay": renewal_day, "unassignedTokens": unassigned}


__all__ = ["summarize_periods"]
