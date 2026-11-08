"""Pure calendar-period summaries from sanitized, event-dated token counters."""
from __future__ import annotations

import calendar
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pricing import credit_estimate_summary, estimate_turn, validated_slices, merge_speed_breakdowns, safe_model
from usage_log import _Turn, _override_windows, _timestamp


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


def validated_subscription_period(value):
    """Validate an optional fixed interval, without depending on system tzdata.

    Beijing and UTC are fixed-offset zones so native Windows installations can
    use the setting without adding packages. Other IANA zones require the
    standard library's timezone database to be available.
    """
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"startAt", "endAt", "timeZone"}:
        raise ValueError("订阅周期需要开始时间、结束时间和时区。")
    name = value["timeZone"]
    if not isinstance(name, str) or not name or len(name) > 80:
        raise ValueError("订阅周期时区无效。")
    try:
        zone = {"Asia/Shanghai": timezone(timedelta(hours=8)), "UTC": timezone.utc}.get(name)
        if zone is None:
            zone = ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError):
        raise ValueError("订阅周期时区不可用。") from None
    dates = []
    for key in ("startAt", "endAt"):
        raw = value[key]
        if not isinstance(raw, str) or len(raw) > 40:
            raise ValueError("订阅周期时间无效。")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if (parsed.tzinfo is None or parsed.utcoffset() is None or parsed.timestamp() < 0
                    or parsed.year >= 9999):
                raise ValueError
            dates.append(parsed.astimezone(zone))
        except (ValueError, OverflowError, OSError):
            raise ValueError("订阅周期时间必须包含有效时区。") from None
    start, end = dates
    elapsed = end.astimezone(timezone.utc) - start.astimezone(timezone.utc)
    if not timedelta(0) < elapsed <= timedelta(days=366):
        raise ValueError("订阅周期结束时间必须晚于开始时间，且范围不能超过 366 天。")
    return {"startAt": start.isoformat(), "endAt": end.isoformat(), "timeZone": name}


def _local_interval_days(start, end):
    """The date envelope follows the same local calendar as existing charts."""
    first = datetime.fromtimestamp(start.timestamp()).date()
    # Subtract in UTC, since subtraction on a DST zone follows wall time.
    last_second = end.astimezone(timezone.utc) - timedelta(microseconds=1)
    last = datetime.fromtimestamp(last_second.timestamp()).date() + timedelta(days=1)
    return first, last


def select_subscription_turns(turns, settings):
    """Clip observed usage to a fixed interval without allocating unknown time.

    Original turns and durable event segments are never changed. The returned
    turns are display-only projections: exact timestamped increments retain
    their own model, speed and context. Legacy day-only observations can be
    selected only when the entire local day lies within the fixed interval.
    Unknown timestamps or intersecting boundary days remain unassigned.
    """
    settings = settings if isinstance(settings, dict) else {}
    period = validated_subscription_period(settings.get("subscriptionPeriod"))
    if period is None:
        return None
    start, end = (datetime.fromisoformat(period[key]) for key in ("startAt", "endAt"))
    start_stamp, end_stamp = start.timestamp(), end.timestamp()
    first, last = _local_interval_days(start, end)
    selected, unassigned, gap = [], 0, not isinstance(turns, (list, tuple))
    unique = {}
    for index, turn in enumerate(turns if isinstance(turns, (list, tuple)) else []):
        if not isinstance(turn, dict):
            gap = True
            continue
        key = ((turn["threadId"], turn["id"]) if
               isinstance(turn.get("threadId"), str) and isinstance(turn.get("id"), str)
               else ("anonymous", index))
        unique[key] = turn
    windows = _override_windows(settings.get("speedOverrideWindows"))
    for turn in unique.values():
        total = _counters(turn.get("tokens"))
        if total is None:
            gap = True
            continue
        segments = turn.get("usageSegments")
        segment_sum, valid_segments = _zero(), isinstance(segments, list)
        if valid_segments:
            for segment in segments:
                amount = _counters(segment.get("tokens")) if isinstance(segment, dict) else None
                if amount is None:
                    valid_segments = False
                    break
                _add(segment_sum, amount)
            valid_segments &= segment_sum == total
        if valid_segments:
            rebuilt = _Turn(turn.get("id"), turn.get("threadId"), turn.get("startedAt"),
                            override_windows=windows)
            for segment in segments:
                stamp = _timestamp(segment.get("timestamp"))
                if stamp is None:
                    unassigned += segment["tokens"]["total"]
                    continue
                if start_stamp <= stamp < end_stamp:
                    status = segment.get("pricingMetadataStatus")
                    api_context = segment.get("apiContext")
                    if api_context is not None and (not isinstance(api_context, str) or
                                                    api_context not in {"short", "long", "unknown"}):
                        api_context = "unknown"
                    rebuilt.add(segment["tokens"], stamp, metadata={
                        "model": safe_model(segment.get("model")),
                        "serviceTier": safe_model(segment.get("serviceTier")),
                        "pricingMetadataStatus": status if isinstance(status, str) and status in {"known", "unknown", "mixed"} else "unknown",
                    }, api_context=api_context)
            if not rebuilt.tokens or not rebuilt.tokens["total"]:
                continue
            projected = deepcopy({key: value for key, value in turn.items() if key not in
                                  {"usageSegments", "usageSlices", "dailyUsage", "undatedTokens", "tokens"}})
            projected.update(tokens=dict(rebuilt.tokens),
                             dailyUsage={day: dict(amount) for day, amount in rebuilt.daily_usage.items()},
                             undatedTokens=_zero(),
                             usageSlices=[{"day": day, "speedOverrideDay": override_day, "model": model,
                                           "serviceTier": tier, "pricingMetadataStatus": status,
                                           "apiContext": context, "tokens": dict(amount)}
                                          for (day, override_day, model, tier, status, context, _cache_write), amount
                                          in rebuilt.usage_slices.items()])
            selected.append(projected)
            continue
        # An invalid event stream cannot be used for precise slicing. Existing
        # coherent day buckets still retain their known, fully covered portion.
        gap |= "usageSegments" in turn
        prepared, undated, bad_days = _prepare([turn])
        unassigned += undated
        gap |= bad_days
        if not prepared:
            continue
        _, daily = prepared[0]
        included, amount = {}, _zero()
        for day, tokens in daily.items():
            if not first <= day < last:
                continue
            midnight = datetime.combine(day, datetime.min.time()).astimezone()
            tomorrow = datetime.combine(day + timedelta(days=1), datetime.min.time()).astimezone()
            day_start, day_end = midnight.timestamp(), tomorrow.timestamp()
            if start_stamp <= day_start and day_end <= end_stamp:
                included[day.isoformat()] = dict(tokens)
                _add(amount, tokens)
            elif day_start < end_stamp and start_stamp < day_end:
                unassigned += tokens["total"]
        if not amount["total"]:
            continue
        projected = deepcopy({key: value for key, value in turn.items() if key not in
                              {"usageSegments", "usageSlices", "dailyUsage", "undatedTokens", "tokens"}})
        projected.update(tokens=amount, dailyUsage=included, undatedTokens=_zero())
        try:
            slices = validated_slices(turn)
        except (ValueError, TypeError, OverflowError):
            slices = None
            projected.update(model=None, serviceTier=None, pricingMetadataStatus="unknown", quality="partial")
            gap = True
        if slices is not None:
            projected["usageSlices"] = [item for item in slices if item["day"] in included]
        selected.append(projected)
    return {**period, "source": "custom", "endExclusive": True,
            "startDate": first.isoformat(), "endDate": last.isoformat(),
            "turns": selected, "unassignedTokens": unassigned,
            "coverageGap": bool(gap or unassigned)}


def _format(value):
    return format(value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP), "f")


def _credit_accumulator(partial=False):
    return {"total": Decimal(0), "available": False, "unpricedTokens": 0,
            "partial": partial, "dates": set(), "mixedDates": False, "historical": False, "speedBreakdown": []}


def _add_credit(target, estimate):
    if estimate["credits"] is not None:
        target["total"] += Decimal(estimate["credits"])
        target["available"] = True
    target["speedBreakdown"].extend(estimate.get("speedBreakdown", []))
    target["unpricedTokens"] += estimate["unpricedTokens"]
    target["partial"] |= estimate["status"] != "estimated"
    if estimate["rateDate"]:
        target["dates"].add(estimate["rateDate"])
    elif estimate["credits"] is not None:
        target["mixedDates"] = True
    target["historical"] |= estimate["historical"]


def _finish_credit(target, settings, *, rates=None, empty=False):
    dates = target["dates"]
    return credit_estimate_summary(
        _format(target["total"]) if target["available"] or empty else None,
        unpriced_tokens=target["unpricedTokens"], partial=target["partial"], rates=rates,
        rate_date=next(iter(dates)) if len(dates) == 1 and not target["mixedDates"] else None,
        historical=target["historical"], estimate_basis="period",
        speed_mode=settings.get("speedMode", "auto"),
        speed_breakdown=merge_speed_breakdowns(target["speedBreakdown"]),
        note="本时段暂无已记录用量。" if empty else None)


def _period(prepared, settings, start, end, *, unassigned, coverage_gap):
    totals = _zero()
    money = {key: Decimal(0) for key in _MONEY_FIELDS}
    available = dict.fromkeys(_MONEY_FIELDS, False)
    models = {}
    turn_count = unpriced_tokens = unpriced_turns = uncertain_context_tokens = long_context_tokens = 0
    partial = bool(coverage_gap or unassigned)
    credit = _credit_accumulator(partial)
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
        _add_credit(credit, estimate["creditEstimate"])
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
                "credit": _credit_accumulator(bool(coverage_gap or unassigned)),
                "creditRates": row["creditEstimate"]["standardRates"],
            })
            _add_credit(group["credit"], row["creditEstimate"])
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
        "creditEstimate": _finish_credit(credit, settings, empty=not turn_count and not credit["partial"]),
        "models": [
            {key: group[key] for key in ("model", "label", "standardRates", "tokens", "unpricedTokens", "apiContextUncertainTokens", "apiLongContextTokens", "partial", "turnCount")} |
            {key: _format(group["money"][key]) if group["available"][key] else None for key in _MONEY_FIELDS} |
            {"creditEstimate": _finish_credit(group["credit"], settings, rates=group["creditRates"])}
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


def summarize_periods(turns, settings, *, now=None, reading_incomplete=False, read_error_count=0,
                      subscription_selection=None):
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
        custom = (subscription_selection if subscription_selection is not None else
                  select_subscription_turns(turns, settings))
        if settings.get("subscriptionPeriod") is not None and custom is not None:
            clipped, missing, clipped_gap = _prepare(custom["turns"])
            subscription = _period(clipped, settings,
                                   date.fromisoformat(custom["startDate"]), date.fromisoformat(custom["endDate"]),
                                   unassigned=custom["unassignedTokens"] + missing,
                                   coverage_gap=bool(gap or custom["coverageGap"] or clipped_gap))
            subscription.update({key: custom[key] for key in
                                 ("startAt", "endAt", "timeZone", "source", "endExclusive")})
            subscription["note"] = ("按自定义起止时刻汇总，包含开始时刻、不包含结束时刻；日期仍按本机日历展示。 " +
                                    subscription["note"])
            if subscription["unassignedTokens"]:
                subscription["note"] += " 部分 Token 缺少足够精确的事件时间，无法确定是否位于边界内，未计入自定义周期。"
        elif renewal_day is None:
            subscription = {
                "startDate": None, "endDate": None, "status": "needs_configuration", "tokens": None,
                "amount": None, "credits": None, "usd": None, "unpricedTokens": 0,
                "unpricedTurnCount": 0, "unassignedTokens": unassigned,
                "partial": bool(gap or unassigned), "turnCount": 0,
                "models": [],
                "creditEstimate": credit_estimate_summary(
                    None, partial=bool(gap or unassigned),
                    note="先设置每月续订日，才能汇总当前订阅周期。"),
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


__all__ = ["summarize_periods", "validated_subscription_period", "select_subscription_turns"]
