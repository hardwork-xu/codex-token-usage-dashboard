"""Allowlisted server credit observations, independent of local token pricing.

Balances are authoritative snapshots, not a transaction ledger. ThreadUsage is
explicitly a server estimate. Neither a balance delta nor a token estimate can
establish how much of a grant was spent rather than expired or adjusted.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext
from datetime import datetime
import hashlib
import math
import re
import time

SOURCE_URL = "https://chatgpt.com/settings/usage?tab=analytics"
STALE_SECONDS = 120
MAX_HISTORY = 2000
USD_PER_CREDIT = Decimal("0.04")


def timestamp(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and 0 < value <= 253402300799 and math.isfinite(value))


def credit_number(value):
    """Preserve server decimal precision, reject exponent/NaN/huge payloads."""
    if not isinstance(value, str) or not re.fullmatch(r"-?\d{1,19}(?:\.\d{1,12})?", value):
        return None
    return value


def account_scope(value):
    if not isinstance(value, str) or not value or len(value) > 256:
        return None
    # Keep the opaque account ID out of the UI and persisted observations.
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_credits(value):
    value = value if isinstance(value, dict) else {}
    return {"balance": credit_number(value.get("balance")),
            "hasCredits": value.get("hasCredits") if type(value.get("hasCredits")) is bool else None,
            "unlimited": value.get("unlimited") if type(value.get("unlimited")) is bool else None}


def credits_from_response(value):
    """Use the main bucket once; never add duplicated model bucket balances."""
    buckets = value.get("rateLimitsByLimitId")
    if isinstance(buckets, dict) and buckets:
        bucket = buckets.get("codex")
    else:
        bucket = value.get("rateLimits")
        if isinstance(bucket, dict) and bucket.get("limitId") not in (None, "codex"):
            bucket = None
    return normalize_credits(bucket.get("credits") if isinstance(bucket, dict) else None)


def _observations(value):
    rows = value if isinstance(value, list) else []
    result = []
    for row in rows[-MAX_HISTORY:]:
        if (isinstance(row, dict) and timestamp(row.get("observedAt"))
                and credit_number(row.get("balance")) is not None):
            if not result or row["observedAt"] > result[-1]["observedAt"]:
                result.append({"observedAt": row["observedAt"], "balance": row["balance"]})
    return result


def record_balance(ledger, quota):
    """Retain bounded balance changes per account, without inferring spend."""
    scope = quota.get("accountScope")
    balance = normalize_credits(quota.get("credits"))["balance"]
    now = quota.get("updatedAt")
    ledger = dict(ledger) if isinstance(ledger, dict) else {}
    if not scope or balance is None or not timestamp(now) or quota.get("error"):
        return ledger
    previous = ledger.get(scope)
    previous = previous if isinstance(previous, dict) else {}
    rows = _observations(previous.get("history"))
    baseline = _observations([previous.get("baseline")])
    point = {"observedAt": now, "balance": balance}
    if rows and now < rows[-1]["observedAt"]:
        return ledger
    new_day = False
    if rows:
        try:
            new_day = datetime.fromtimestamp(now).date() != datetime.fromtimestamp(rows[-1]["observedAt"]).date()
        except (ValueError, OverflowError, OSError):
            pass
    # Retain the first actual observation each local day even without a balance
    # change. Repeated same-day polls stay deduplicated; no midnight is inferred.
    if not rows or new_day or Decimal(rows[-1]["balance"]) != Decimal(balance):
        if rows and now == rows[-1]["observedAt"]:
            rows[-1] = point
        else:
            rows.append(point)
    ledger[scope] = {"baseline": baseline[0] if baseline else point,
                     "history": rows[-MAX_HISTORY:]}
    return ledger


def _today_observation(entry, quota, *, now):
    """Compare actual same-day observations, never infer a midnight balance."""
    result = {"status": "unavailable", "startAt": None, "endAt": None,
              "startBalance": None, "endBalance": None, "netChange": None,
              "note": "今天还没有足够的账户余额观测；不会推算零点余额或实际消耗。"}
    balance = normalize_credits(quota.get("credits"))
    updated = quota.get("updatedAt")
    if (not isinstance(entry, dict) or not timestamp(now) or not timestamp(updated)
            or updated > now or balance["balance"] is None or balance["unlimited"] is True):
        return result
    raw = entry.get("history")
    if not isinstance(raw, list):
        return result
    # Fail closed for this comparison instead of quietly sorting corrupt data
    # into a plausible change. The persisted ledger itself is not modified.
    points = []
    baseline = entry.get("baseline")
    if baseline is not None:
        points.append(baseline)
    for index, row in enumerate(raw[-MAX_HISTORY:]):
        if index == 0 and points and row == points[-1]:
            continue
        points.append(row)
    checked = []
    for row in points:
        if (not isinstance(row, dict) or not timestamp(row.get("observedAt"))
                or row["observedAt"] > now or credit_number(row.get("balance")) is None
                or checked and row["observedAt"] <= checked[-1]["observedAt"]):
            return result
        checked.append({"observedAt": row["observedAt"], "balance": row["balance"]})
    current = {"observedAt": updated, "balance": balance["balance"]}
    if checked and updated <= checked[-1]["observedAt"]:
        if (updated != checked[-1]["observedAt"]
                or Decimal(balance["balance"]) != Decimal(checked[-1]["balance"])):
            return result
    else:
        checked.append(current)
    try:
        day = datetime.fromtimestamp(now).date()
        today = [row for row in checked if datetime.fromtimestamp(row["observedAt"]).date() == day]
    except (ValueError, OverflowError, OSError):
        return result
    if len(today) < 2:
        return result
    start, end = today[0], today[-1]
    with localcontext() as context:
        context.prec = 60
        change = format(Decimal(end["balance"]) - Decimal(start["balance"]), "f")
    result.update(status="observed", startAt=start["observedAt"], endAt=end["observedAt"],
                  startBalance=start["balance"], endBalance=end["balance"], netChange=change,
                  note="仅比较今天实际观测时段内的余额净变化，不是零点至今的消耗；可能包含充值、到期或调整。")
    return result


def credit_view(quota, ledger, settings, *, now, validated=False):
    result = normalize_credits(quota.get("credits"))
    updated = quota.get("updatedAt")
    available = result["balance"] is not None or result["unlimited"] is True
    fresh = (timestamp(updated) and validated and not quota.get("error")
             and updated <= now + 5 and now < updated + STALE_SECONDS)
    result.update(status=("fresh" if fresh else "stale") if available else "unavailable",
                  updatedAt=updated if timestamp(updated) else None,
                  error="官方 Credits 同步失败，请刷新或查看官方 Usage 页面" if quota.get("error") else None, sourceUrl=SOURCE_URL,
                  usdEquivalent=None, amountEquivalent=None,
                  history=[], trackingStartAt=None, trackingStartBalance=None, netChange=None,
                  note="官方账户余额，含适用的赠送或购买 Credits。余额净变化也可能包含充值、到期或调整，不能视作实际消耗明细。")
    if result["balance"] is not None and result["unlimited"] is not True:
        with localcontext() as context:
            context.prec = 60
            usd = Decimal(result["balance"]) * USD_PER_CREDIT
            result["usdEquivalent"] = format(usd, "f")
            # A user's arbitrary custom unit has no guaranteed currency mapping.
            if settings.get("currencyCode") in {"USD", "CNY", "HKD"}:
                try:
                    rate = Decimal(str(settings.get("currencyPerUsd", "1")))
                    if rate.is_finite() and 0 < rate <= 1_000_000_000:
                        result["amountEquivalent"] = format(usd * rate, "f")
                except InvalidOperation:
                    pass
    scope = quota.get("accountScope")
    entry = ledger.get(scope) if isinstance(ledger, dict) and scope else None
    result["todayObservation"] = _today_observation(entry, quota, now=now)
    if isinstance(entry, dict):
        rows = _observations(entry.get("history"))
        baseline = _observations([entry.get("baseline")])
        result["history"] = rows[-30:]
        if baseline and result["balance"] is not None and timestamp(updated) and updated >= baseline[0]["observedAt"]:
            result.update(trackingStartAt=baseline[0]["observedAt"], trackingStartBalance=baseline[0]["balance"])
            with localcontext() as context:
                context.prec = 60
                result["netChange"] = format(Decimal(result["balance"]) - Decimal(baseline[0]["balance"]), "f")
    return result


def _counter(value):
    return value if type(value) is int and 0 <= value <= 10**30 else None


def _micros(value):
    value = _counter(value)
    if value is None:
        return None
    # Avoid binary floats and Decimal's default precision rounding large totals.
    return f"{value // 1_000_000}.{value % 1_000_000:06d}"


def _label(value):
    return value if isinstance(value, str) and 0 < len(value) <= 160 and all(ord(c) >= 32 for c in value) else None


def normalize_thread_usage(value, thread_id):
    result = {"status": "unavailable", "updatedAt": time.time(), "error": None,
              "credits": None, "usd": None, "groups": [], "sourceUrl": SOURCE_URL,
              "note": "官方暂未提供此对话的用量记录；本地估算不会补成官方消耗。"}
    usage = value.get("threadUsage") if isinstance(value, dict) else None
    if not isinstance(usage, dict) or usage.get("threadId") != thread_id:
        return result
    credits = _micros(usage.get("estimatedUsageCreditsMicros"))
    if credits is None:
        return result
    rows = usage.get("groups")
    if not isinstance(rows, list) or len(rows) > 500:
        return result
    groups = []
    for row in rows:
        if not isinstance(row, dict) or _micros(row.get("estimatedUsageCreditsMicros")) is None:
            continue
        groups.append({"model": _label(row.get("model")),
                       "reasoningEffort": _label(row.get("reasoningEffort")),
                       "speed": _label(row.get("speed")),
                       "credits": _micros(row["estimatedUsageCreditsMicros"]),
                       "tokens": {key: _counter(row.get(source)) for key, source in (
                           ("input", "inputTokens"), ("cachedInput", "cachedInputTokens"),
                           ("output", "outputTokens"), ("total", "totalTokens"))}})
    result.update(status="fresh", credits=credits, usd=_micros(usage.get("estimatedUsageUsdMicros")), groups=groups,
                  note="官方服务返回的整段对话估计用量，保留服务器数值；不是当前问题、今日总量或 Credits 钱包的实际扣除。")
    return result


def fetch_thread_usage(thread_id, command):
    from rpc_transport import JsonRpcProcess
    try:
        with JsonRpcProcess([*command, "app-server", "--stdio"], timeout=20) as rpc:
            rpc.send({"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "codex_usage_meter", "version": "0.12.0"}}})
            initialized = False
            for response in rpc.responses():
                if type(response.get("id")) is not int:
                    continue
                if not initialized and response["id"] == 1:
                    if "error" in response or not isinstance(response.get("result"), dict):
                        raise RuntimeError("官方用量初始化失败，请检查 Codex 登录状态")
                    initialized = True
                    rpc.send({"method": "initialized"})
                    rpc.send({"id": 2, "method": "account/usage/read", "params": {"threadId": thread_id}})
                elif initialized and response["id"] == 2:
                    if "error" in response:
                        raise RuntimeError("官方用量查询未成功；请在官方 Usage 页面核对")
                    return normalize_thread_usage(response.get("result"), thread_id)
    except (OSError, TimeoutError):
        raise RuntimeError("官方用量连接暂不可用，请稍后重试") from None
    raise RuntimeError("官方用量连接已关闭")


def thread_usage_view(record, *, now, validated=False):
    record = record if isinstance(record, dict) else {}
    def amount(value):
        return value if isinstance(value, str) and re.fullmatch(r"\d{1,25}\.\d{6}", value) else None
    result = {"status": "fresh", "updatedAt": record.get("updatedAt") if timestamp(record.get("updatedAt")) else None,
              "credits": amount(record.get("credits")), "usd": amount(record.get("usd")), "groups": [],
              "error": "官方用量同步失败，请稍后重试或查看官方 Usage 页面" if record.get("error") else None,
              "sourceUrl": SOURCE_URL,
              "note": "官方服务返回的整段对话估计用量；不是当前问题、今日总量或 Credits 钱包的实际扣除。"}
    rows = record.get("groups")
    for row in rows[:500] if isinstance(rows, list) else []:
        if not isinstance(row, dict) or amount(row.get("credits")) is None:
            continue
        tokens = row.get("tokens") if isinstance(row.get("tokens"), dict) else {}
        result["groups"].append({**{key: _label(row.get(key)) for key in ("model", "reasoningEffort", "speed")},
                                 "credits": row["credits"],
                                 "tokens": {key: _counter(tokens.get(key)) for key in ("input", "cachedInput", "output", "total")}})
    if result["credits"] is None:
        result.update(status="unavailable", credits=None, usd=None, groups=[],
                      note="官方暂未提供此对话的用量记录；本地估算不会补成官方消耗。")
    elif (not validated or result.get("error") or not timestamp(result.get("updatedAt"))
          or result["updatedAt"] > now + 5 or now >= result["updatedAt"] + STALE_SECONDS):
        result["status"] = "stale"
    return result
