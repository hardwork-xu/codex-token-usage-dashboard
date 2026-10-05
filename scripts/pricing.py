"""Pure Decimal estimates from observed tokens; never an account charge or balance.

API replacement costs and the ChatGPT/Codex credit schedule use separate dated
tables. Only explicitly supported model IDs are matched; aliases are not guessed.
"""
from __future__ import annotations

from decimal import Decimal, DecimalException, ROUND_HALF_UP, localcontext
from datetime import date
import re
from typing import Any


SOURCE_URL = "https://learn.chatgpt.com/docs/pricing"
SPEED_SOURCE_URL = "https://learn.chatgpt.com/docs/agent-configuration/speed"
RATE_DATE = "2026-09-30"
API_SOURCE_URL = "https://developers.openai.com/api/docs/pricing"
API_RATE_DATE = "2026-09-30"
HISTORICAL_RATE_DATE = "2026-09-27"
_MILLION = Decimal(1_000_000)
_DISPLAY = Decimal("0.000001")
_MAX_COUNTER = 10**30
# Preserve the previous plugin snapshot for audit, with its original sources.
# It is not a billing ledger and is never selected from a usage event's date.
# Tuple fields are label / uncached input / cached input / output per million.
_CREDIT_RATES_2026_09_27 = {
    "gpt-6-sol": ("GPT-6 Sol", "50", "5", "250"),
    "gpt-6-luna": ("GPT-6 Luna", "2.5", "0.25", "12.5"),
    "gpt-6-astra": ("GPT-6 Astra", "250", "25", "1250"),
    "gpt-5.6-sol": ("GPT-5.6 Sol", "100", "10", "500"),
    "gpt-5.6-terra": ("GPT-5.6 Terra", "50", "5", "300"),
    "gpt-5.6-luna": ("GPT-5.6 Luna", "5", "0.5", "30"),
    "gpt-5.5": ("GPT-5.5", "125", "12.5", "750"),
    "gpt-5.4": ("GPT-5.4", "62.5", "6.25", "375"),
    "gpt-5.4-mini": ("GPT-5.4 mini", "18.75", "1.875", "113"),
}
_API_RATES_2026_09_27 = {
    "gpt-6-astra": ("GPT-6 Astra", "10", "1", "50"),
    "gpt-6-sol": ("GPT-6 Sol", "2", "0.2", "10"),
    "gpt-6-luna": ("GPT-6 Luna", "0.1", "0.01", "0.5"),
    "gpt-5.6-sol": ("GPT-5.6 Sol", "4", "0.4", "20"),
    "gpt-5.6-terra": ("GPT-5.6 Terra", "2", "0.2", "12"),
    "gpt-5.6-luna": ("GPT-5.6 Luna", "0.2", "0.02", "1.2"),
    "gpt-5.5": ("GPT-5.5", "5", "0.5", "30"),
    "gpt-5.4": ("GPT-5.4", "2.5", "0.25", "15"),
    "gpt-5.4-mini": ("GPT-5.4 mini", "0.75", "0.075", "4.5"),
}
HISTORICAL_RATE_SNAPSHOTS = {
    HISTORICAL_RATE_DATE: {
        "rateDate": HISTORICAL_RATE_DATE,
        "creditSourceUrl": SOURCE_URL, "apiSourceUrl": API_SOURCE_URL,
        "speedSourceUrl": SPEED_SOURCE_URL,
        "creditRates": dict(_CREDIT_RATES_2026_09_27),
        "apiRates": dict(_API_RATES_2026_09_27),
        "apiModelSourceUrls": {model: "https://developers.openai.com/api/docs/models/" + model
                               for model in _API_RATES_2026_09_27},
        "creditFastMultipliers": {
            "gpt-6-sol": "2.5", "gpt-6-luna": "2.5", "gpt-6-astra": "2.5",
            "gpt-5.6-sol": "2.5", "gpt-5.6-terra": "2.5", "gpt-5.6-luna": "2.5",
            "gpt-5.5": "2.5", "gpt-5.4": "2",
        },
    },
}
# September 30 Standard credit rates, with two explicitly dated legacy rows.
# GPT-5.4/mini are no longer on the current credit table: do not relabel them
# as newly verified. The remaining previous rows were verified unchanged.
RATES = {**_CREDIT_RATES_2026_09_27, "gpt-6.1-sol": ("GPT-6.1 Sol", "50", "2.5", "250")}
HISTORICAL_CREDIT_MODELS = frozenset({"gpt-5.4", "gpt-5.4-mini"})
# Independently verified Standard API USD prices, never credits times a conversion.
# All previous API rows remain published on their individual model pages.
API_RATES = {**_API_RATES_2026_09_27, "gpt-6.1-sol": ("GPT-6.1 Sol", "2", "0.1", "10")}
API_LONG_CONTEXT_MODELS = frozenset(API_RATES) - {"gpt-5.4-mini"}
API_LONG_CONTEXT_THRESHOLD = 272_000
# Purchased-credit billing only; included subscription limits use different
# multipliers (Fast 2.5x, Astra Ultrafast 8x) and cannot be inferred from money.
FAST_MULTIPLIERS = {model: Decimal(2) for model in (
    "gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna",
    "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4",
)}
ULTRAFAST_MULTIPLIERS = {"gpt-6-astra": Decimal(6)}


def speed_evidence(turn, settings):
    """Prefer observed tiers, then a dated user declaration, then a fallback.

    A current account preference cannot prove a historical request's speed.
    Dates come only from conserved usage slices, never from the turn start.
    API priority/default are not aliases for Codex Fast/Standard.
    """
    supported = {"standard", "fast", "ultrafast"}
    tier = turn.get("serviceTier")
    if turn.get("pricingMetadataStatus") == "known" and isinstance(tier, str) and tier in supported:
        return tier, "recorded"
    day = turn.get("day")
    overrides = settings.get("speedOverrides")
    if isinstance(day, str) and isinstance(overrides, dict):
        try:
            valid_day = date.fromisoformat(day).isoformat() == day
        except ValueError:
            valid_day = False
        if valid_day and isinstance(overrides.get(day), str) and overrides[day] in supported:
            return overrides[day], "date_override"
    fallback = settings.get("speedMode", "auto")
    return fallback if isinstance(fallback, str) else "invalid", "fallback"


def merge_speed_breakdowns(rows):
    counts = {}
    for row in rows:
        key = (row["speed"], row["source"])
        counts[key] = counts.get(key, 0) + row["tokens"]
    return [{"speed": speed, "source": source, "tokens": count}
            for (speed, source), count in sorted(counts.items()) if count]


def _speed_rows(turn, settings):
    tokens = turn.get("tokens")
    total = tokens.get("total") if isinstance(tokens, dict) else None
    if type(total) is not int or total < 0:
        return []
    speed, source = speed_evidence(turn, settings)
    return [{"speed": speed if speed in {"standard", "fast", "ultrafast"} else "unknown",
             "source": source, "tokens": total}]


def _credit_rate_date(model):
    return HISTORICAL_RATE_DATE if model in HISTORICAL_CREDIT_MODELS else RATE_DATE


def _number(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)) or len(str(value)) > 60:
        raise ValueError("换算设置无效。")
    result = Decimal(str(value))
    if not result.is_finite() or not 0 <= result <= 1_000_000_000:
        raise ValueError("换算设置无效。")
    if result and result.adjusted() < -30:
        raise ValueError("换算设置精度超出支持范围。")
    return result if result else Decimal(0)


def _counter(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_COUNTER:
        raise ValueError("Token 数据无效或超出支持范围。")
    return value


def _text(value: Decimal) -> str:
    return format(value.quantize(_DISPLAY, rounding=ROUND_HALF_UP), "f")


def _base(turn: dict, official: bool, *, api: bool = False) -> dict:
    model = turn.get("model") if isinstance(turn.get("model"), str) else None
    return {
        "status": "unavailable", "credits": None, "creditsMax": None,
        "usd": None, "usdMax": None, "amount": None, "amountMax": None,
        "note": "", "model": model,
        "label": "API 替代成本估算" if api else "购买 credits 费率估算" if official else "自定义金额换算",
        "sourceUrl": (_api_source(model) if api else SOURCE_URL) if official else None,
        "rateDate": (API_RATE_DATE if api else _credit_rate_date(model)) if official else None,
        "historical": bool(official and not api and model in HISTORICAL_CREDIT_MODELS),
        "billingBasis": "api_standard" if api else "purchased_credits" if official else "custom",
        "breakdown": None, "estimateBasis": None,
        "apiContextUncertainTokens": 0, "apiLongContextTokens": 0,
    }


def _api_source(model):
    return "https://developers.openai.com/api/docs/models/" + model if model in API_RATES else API_SOURCE_URL


def _api_context(value):
    return value if isinstance(value, str) and value in {"short", "long", "unknown"} else "unknown"


def _price_api(turn, settings, result, incoming, cached, outgoing, total, money_text):
    """Use upstream per-request/session evidence, never aggregate turn length.

    GPT-5.5/5.4 document a session-wide threshold; callers must supply evidence
    appropriate to that scope. Missing scope evidence stays unknown. Cache-write
    counters have already been rejected because their log/API mapping is unproven.
    """
    model = result["model"]
    currency_per_usd = _number(settings.get("currencyPerUsd", "1"))
    context = _api_context(turn.get("apiContext"))
    long_supported = model in API_LONG_CONTEXT_MODELS
    input_multiplier = output_multiplier = Decimal(1)
    result["estimateBasis"] = "api_standard"
    notes = ["按各模型 API Standard 文本 Token 单价估算替代成本，不含工具调用等非 Token 费用。"]
    if long_supported and context == "long":
        input_multiplier, output_multiplier = Decimal(2), Decimal("1.5")
        result["estimateBasis"] = "api_long"
        result["apiLongContextTokens"] = total
        notes.append("按已确认的长上下文计价。")
    elif long_supported and context == "unknown":
        input_multiplier, output_multiplier = Decimal("1.5"), Decimal("1.25")
        result["estimateBasis"] = "api_context_midpoint"
        result["apiContextUncertainTokens"] = total
        notes.append("上下文长度缺少足够证据，按短与长上下文成本的中点估算。")
    components = {}
    usd = Decimal(0)
    for field, count, raw_rate, multiplier in zip(
        ("uncachedInput", "cachedInput", "output"),
        (incoming - cached, cached, outgoing), API_RATES[model][1:],
        (input_multiplier, input_multiplier, output_multiplier),
    ):
        component = Decimal(count) * Decimal(raw_rate) * multiplier / _MILLION
        usd += component
        components[field] = {"tokens": count, "usdPerMillion": raw_rate,
                             "contextMultiplier": str(multiplier), "usd": money_text(component)}
    result.update(usd=money_text(usd), amount=money_text(usd * currency_per_usd))
    result["breakdown"] = {**components, "processing": "standard", "apiContext": context,
                           "currencyPerUsd": str(currency_per_usd),
                           "longContextThreshold": API_LONG_CONTEXT_THRESHOLD if long_supported else None}
    notes.append("仅用于比较，不代表实际 API 账单。")
    return _finish(result, turn, notes)


def _finish(result: dict, turn: dict, notes: list[str]) -> dict:
    result["status"] = "estimated"
    if turn.get("quality") == "partial":
        result["status"] = "partial"
        notes.append("Token 记录不完整，仅估算已记录部分。")
    if turn.get("status") == "running":
        notes.append("本题仍在运行，这是截至目前的估算。")
    notes.append("不代表订阅实际扣款或剩余额度。")
    result["note"] = " ".join(notes)
    return result


def _estimate_single(turn: dict, settings: dict, *, _unrounded=False) -> dict:
    """Return a stable, JSON-safe estimate; unknown inputs fail closed.

    `amount` uses the selected currency. API mode computes USD directly from
    separate Standard API token rates and ignores Codex credit/speed settings.
    Official mode converts credits by configured USD per credit and retains its
    explicit speed scenarios. Ambiguous API context or legacy speed uses an
    unrounded midpoint before FX; public max fields stay null. Reasoning effort
    changes no rate, and reasoning tokens remain a subset of output tokens.
    """
    turn = turn if isinstance(turn, dict) else {}
    settings = settings if isinstance(settings, dict) else {}
    mode = settings.get("pricingMode", "official")
    result = _base(turn, mode != "custom", api=mode == "api")
    money_text = (lambda value: format(value, "f")) if _unrounded else _text
    try:
        if not isinstance(mode, str) or mode not in {"official", "custom", "api"}:
            raise ValueError("计价方式无效。")
        tokens = turn.get("tokens")
        if not isinstance(tokens, dict) or turn.get("quality") == "unavailable":
            raise ValueError("暂无可用的 Token 记录。")
        total = _counter(tokens.get("total"))
        with localcontext() as context:
            context.prec = 100
            context.Emax = 1000
            context.Emin = -1000
            if mode == "custom":
                raw_rate = settings.get("ratePerMillion")
                if raw_rate is None or raw_rate == "":
                    raise ValueError("请先设置每百万 Token 的自定义换算单价。")
                rate = _number(raw_rate)
                result["amount"] = money_text(Decimal(total) * rate / _MILLION)
                result["estimateBasis"] = "custom"
                result["breakdown"] = {"totalTokens": total, "ratePerMillion": str(rate)}
                return _finish(result, turn, ["按自定义单价换算，非官方价格。"])

            metadata = turn.get("pricingMetadataStatus")
            if metadata == "mixed":
                raise ValueError("本题混用了模型或服务档位，无法用单一官方费率估算。")
            if not isinstance(metadata, str) or metadata not in {"known", "unknown"}:
                raise ValueError("缺少可验证的模型与服务档位信息。")
            model = result["model"]
            if model == "gpt-5.3-codex-spark":
                raise ValueError("Spark 暂无公开数字费率，不能据此估算金额。")
            if model not in (API_RATES if mode == "api" else RATES):
                raise ValueError("该模型暂无本插件可核实的官方数字费率。")
            incoming = _counter(tokens.get("input"))
            cached = _counter(tokens.get("cachedInput"))
            outgoing = _counter(tokens.get("output"))
            reasoning = _counter(tokens.get("reasoningOutput"))
            cache_write = _counter(tokens.get("cacheWriteInput"))
            if cached > incoming or reasoning > outgoing or total != incoming + outgoing:
                raise ValueError("Token 明细不一致，无法可靠估算。")
            if cache_write:
                if mode == "api":
                    raise ValueError("缓存写入 Token 与 API 计价字段的映射尚未核实，暂不估算。")
                raise ValueError("Codex credits 无独立缓存写入费；此日志计数与输入字段的映射尚未核实，暂不估算。")

            if mode == "api":
                return _price_api(turn, settings, result, incoming, cached, outgoing, total, money_text)

            speed, speed_source = speed_evidence(turn, settings)
            fast = FAST_MULTIPLIERS.get(model)
            ultrafast = ULTRAFAST_MULTIPLIERS.get(model)
            multiplier, multiplier_max = Decimal(1), None
            notes: list[str] = []
            label = {"standard": "Standard（非加速）", "fast": "Fast", "ultrafast": "Ultrafast"}.get(speed)
            if speed in {"standard", "fast", "ultrafast"}:
                if speed == "fast":
                    if fast is None:
                        raise ValueError("该模型没有已核实的 Fast 倍率。")
                    multiplier = fast
                elif speed == "ultrafast":
                    if ultrafast is None:
                        raise ValueError("该模型没有已核实的 Ultrafast 倍率。")
                    multiplier = ultrafast
                result["estimateBasis"] = speed
                if speed_source == "recorded":
                    notes.append("按记录中的 " + label + " 档位估算。")
                elif speed_source == "date_override":
                    notes.append("速度记录缺失，按用户确认的该日 " + label + " 档位估算。")
                else:
                    notes.append("速度记录缺失，按用户选择的 " + label + " 默认场景估算，不据此推断实际档位。")
            elif speed == "auto":
                if fast is not None:
                    multiplier_max = fast
                    result["estimateBasis"] = "midpoint"
                    notes.append("速度档位未确认，仅按 Standard 与 Fast 场景的中点估算，不含 Ultrafast。")
                else:
                    raise ValueError("速度档位不明且没有已核实的 Fast 倍率；可选择 Standard 场景估算。")
            else:
                raise ValueError("速度计价设置无效。")

            usd_per_credit = _number(settings.get("usdPerCredit", "0.04"))
            currency_per_usd = _number(settings.get("currencyPerUsd", "1"))
            components = {}
            standard_credits = Decimal(0)
            for field, count, raw_rate in zip(
                ("uncachedInput", "cachedInput", "output"),
                (incoming - cached, cached, outgoing), RATES[model][1:],
            ):
                rate = Decimal(raw_rate)
                credits = Decimal(count) * rate / _MILLION
                standard_credits += credits
                components[field] = {"tokens": count, "creditsPerMillion": raw_rate, "standardCredits": _text(credits)}
            credits = standard_credits * multiplier
            if multiplier_max is not None:
                credits = (credits + standard_credits * multiplier_max) / 2
                multiplier = (multiplier + multiplier_max) / 2
            usd = credits * usd_per_credit
            result.update(credits=money_text(credits), usd=money_text(usd), amount=money_text(usd * currency_per_usd))
            result["breakdown"] = {
                **components, "multiplier": str(multiplier),
                "multiplierMax": None,
                "usdPerCredit": str(usd_per_credit), "currencyPerUsd": str(currency_per_usd),
                "speedSourceUrl": SPEED_SOURCE_URL, "speedEvidence": speed_source,
                "speedRateDate": _credit_rate_date(model), "billingBasis": "purchased_credits",
            }
            if result["historical"]:
                notes.append("使用 2026-09-27 历史 credits 费率；该模型已不在当前 credits 表中，不能视为现行报价。")
            notes.append("仅按购买 credits 费率比较，不用于推算订阅内含额度或实际扣除记录。")
            notes.append("金额使用设置中的每 credit 美元价值及货币汇率换算。")
            return _finish(result, turn, notes)
    except (ValueError, DecimalException) as exc:
        # None of the untrusted numeric/metadata values are interpolated into
        # output. Preserve safe diagnostics while returning no partial totals.
        result.update(status="unavailable", credits=None, creditsMax=None, usd=None, usdMax=None, amount=None, amountMax=None, breakdown=None, estimateBasis=None, apiContextUncertainTokens=0, apiLongContextTokens=0)
        result["note"] = str(exc) if isinstance(exc, ValueError) else "换算数值超出可支持范围。"
        return result


TOKEN_FIELDS = ("total", "input", "cachedInput", "cacheWriteInput", "output", "reasoningOutput")
MONEY_FIELDS = ("amount", "credits", "usd")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


def safe_model(value):
    return value if isinstance(value, str) and _SAFE_ID.fullmatch(value) else None


def _valid_tokens(value):
    if not isinstance(value, dict):
        raise ValueError("Token 数据无效。")
    # Counting remains lossless even if a counter exceeds the pricing limit.
    # _estimate_single separately rejects values too large to price safely.
    if any(type(value.get(key)) is not int or value[key] < 0 for key in TOKEN_FIELDS):
        raise ValueError("Token 数据无效。")
    result = {key: value[key] for key in TOKEN_FIELDS}
    if (result["total"] != result["input"] + result["output"] or
            result["cachedInput"] > result["input"] or result["reasoningOutput"] > result["output"]):
        raise ValueError("Token 明细不一致。")
    return result


def validated_slices(turn):
    """Validate conservation before using model/date evidence; never repair it by guessing.

    None means a legacy snapshot with no slices. Invalid new snapshots fail closed.
    Only numeric counters, calendar dates and safe model/tier IDs leave this helper.
    """
    if "usageSlices" not in turn:
        return None
    raw = turn["usageSlices"]
    if not isinstance(raw, list):
        raise ValueError("模型用量片段无效。")
    total = _valid_tokens(turn.get("tokens"))
    summed = dict.fromkeys(TOKEN_FIELDS, 0)
    daily = {}
    slices = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("模型用量片段无效。")
        tokens = _valid_tokens(item.get("tokens"))
        day = item.get("day")
        if day is not None and (not isinstance(day, str) or date.fromisoformat(day).isoformat() != day):
            raise ValueError("模型用量日期无效。")
        model, tier = safe_model(item.get("model")), safe_model(item.get("serviceTier"))
        status = "known" if item.get("pricingMetadataStatus") == "known" and model and tier else "unknown"
        slices.append({"day": day, "model": model, "serviceTier": tier,
                       "pricingMetadataStatus": status, "tokens": tokens,
                       "apiContext": _api_context(item.get("apiContext"))})
        bucket = daily.setdefault(day, dict.fromkeys(TOKEN_FIELDS, 0))
        for key in TOKEN_FIELDS:
            summed[key] += tokens[key]
            bucket[key] += tokens[key]
    if summed != total:
        raise ValueError("模型用量与总量不一致。")
    if "dailyUsage" in turn:
        expected = turn["dailyUsage"]
        if not isinstance(expected, dict):
            raise ValueError("模型用量日期无效。")
        # Zero buckets carry no usage and need not appear in either representation.
        expected = {day: _valid_tokens(value) for day, value in expected.items()}
        if {day: value for day, value in daily.items() if day is not None and any(value.values())} != {
                day: value for day, value in expected.items() if any(value.values())}:
            raise ValueError("模型用量与每日总量不一致。")
    return slices


def standard_rates(model, mode="official"):
    """Display metadata from the same table used by the estimator, not a second rate table."""
    rate = (API_RATES if mode == "api" else RATES).get(model)
    if rate is None:
        return None
    metadata = {"uncachedInput": rate[1], "cachedInput": rate[2], "output": rate[3],
                "unit": "usd_per_million_tokens" if mode == "api" else "credits_per_million_tokens",
                "rateDate": API_RATE_DATE if mode == "api" else _credit_rate_date(model),
                "sourceUrl": _api_source(model) if mode == "api" else SOURCE_URL,
                "historical": mode != "api" and model in HISTORICAL_CREDIT_MODELS,
                "billingBasis": "api_standard" if mode == "api" else "purchased_credits"}
    if mode == "api" and model in API_LONG_CONTEXT_MODELS:
        metadata.update(longContextThreshold=API_LONG_CONTEXT_THRESHOLD,
                        longContextInputMultiplier="2", longContextCachedInputMultiplier="2",
                        longContextOutputMultiplier="1.5",
                        longContextScope="session" if model in {"gpt-5.5", "gpt-5.4"} else "request")
    return metadata


def _model_groups(parts, turn, mode="official"):
    groups = {}
    for fragment, price in parts:
        tokens = fragment.get("tokens")
        try:
            tokens = _valid_tokens(tokens)
        except ValueError:
            continue
        if not any(tokens.values()):
            continue
        model = safe_model(fragment.get("model"))
        group = groups.setdefault(model, {
            "model": model, "label": RATES[model][0] if model in RATES else model or "未确认模型",
            "tokens": dict.fromkeys(TOKEN_FIELDS, 0), "turnCount": 1, "partial": False,
            "unpricedTokens": 0, "apiContextUncertainTokens": 0, "apiLongContextTokens": 0,
            "standardRates": standard_rates(model, mode), "speedBreakdown": [], **{key: None for key in MONEY_FIELDS},
        })
        group["speedBreakdown"].extend(price.get("speedBreakdown", []))
        group["apiContextUncertainTokens"] += price.get("apiContextUncertainTokens", 0)
        group["apiLongContextTokens"] += price.get("apiLongContextTokens", 0)
        for key in TOKEN_FIELDS:
            group["tokens"][key] += tokens[key]
        group["partial"] |= (turn.get("quality") != "complete" or bool(turn.get("readingIncomplete"))
                              or price["status"] != "estimated")
        if price["amount"] is None:
            group["unpricedTokens"] += tokens["total"]
        for key in MONEY_FIELDS:
            if price[key] is not None:
                group[key] = (group[key] or Decimal(0)) + Decimal(price[key])
    for group in groups.values():
        group["speedBreakdown"] = merge_speed_breakdowns(group["speedBreakdown"])
        for key in MONEY_FIELDS:
            if group[key] is not None:
                group[key] = _text(group[key])
    return sorted(groups.values(), key=lambda group: (-group["tokens"]["total"], group["model"] is None, group["model"] or ""))


def _estimate_turn(turn: dict, settings: dict) -> dict:
    """Price conserved model slices independently; keep the unpriced remainder visible."""
    turn = turn if isinstance(turn, dict) else {}
    settings = settings if isinstance(settings, dict) else {}
    mode = settings.get("pricingMode", "official")
    invalid = False
    try:
        slices = validated_slices(turn)
    except (ValueError, TypeError, OverflowError):
        slices, invalid = None, True
    if slices is None:
        source = {**turn, "model": safe_model(turn.get("model"))}
        source.pop("day", None)  # Only validated event slices establish a pricing day.
        if invalid:
            source.update(model=None, serviceTier=None, pricingMetadataStatus="unknown", quality="partial")
        price = _estimate_single(source, settings)
        price["speedBreakdown"] = _speed_rows(source, settings)
        with localcontext() as context:
            context.prec = 100
            price["models"] = _model_groups([(source, price)], source, mode)
        price["unpricedTokens"] = sum(group["unpricedTokens"] for group in price["models"])
        if invalid:
            price["note"] = "模型片段校验失败，保留总量并标为未确认模型。 " + price["note"]
        return price

    # Merge days before rounding, but retain different model/tier evidence.
    merged = {}
    for item in slices:
        # Do not spread an unsupported cache-write category to independently
        # observed, priceable calls when merging days or context evidence.
        key = (speed_evidence(item, settings) if mode == "official" else None, item["model"], item["serviceTier"], item["pricingMetadataStatus"], item["apiContext"],
               bool(item["tokens"]["cacheWriteInput"]))
        source = merged.setdefault(key, {**item, "tokens": dict.fromkeys(TOKEN_FIELDS, 0),
                                        "quality": turn.get("quality"), "status": turn.get("status")})
        for field in TOKEN_FIELDS:
            source["tokens"][field] += item["tokens"][field]
    parts = [(source, {**_estimate_single(source, settings, _unrounded=True),
                       "speedBreakdown": _speed_rows(source, settings)})
             for source in merged.values() if any(source["tokens"].values())]
    if not parts:
        price = _estimate_single(turn, settings)
        price.update(models=[], unpricedTokens=0, speedBreakdown=[])
        return price
    with localcontext() as context:
        context.prec = 100
        groups = _model_groups(parts, turn, mode)
        price = dict(parts[0][1]) if len(parts) == 1 else _base(turn, mode != "custom", api=mode == "api")
        price["models"] = groups
        price["speedBreakdown"] = merge_speed_breakdowns([row for group in groups for row in group["speedBreakdown"]])
        price["unpricedTokens"] = sum(group["unpricedTokens"] for group in groups)
        price["apiContextUncertainTokens"] = sum(group["apiContextUncertainTokens"] for group in groups)
        price["apiLongContextTokens"] = sum(group["apiLongContextTokens"] for group in groups)
        if len(parts) == 1:
            for key in MONEY_FIELDS:
                price[key] = groups[0][key]
        if len(parts) > 1:
            for key in MONEY_FIELDS:
                values = [Decimal(group[key]) for group in groups if group[key] is not None]
                price[key] = _text(sum(values)) if values else None
            price["status"] = ("unavailable" if price["amount"] is None else "partial" if
                               any(group["partial"] for group in groups) else "estimated")
            price["estimateBasis"] = "slices"
            price["note"] = "按每段记录中的模型与 Token 类别分别估算并相加；未知模型、缺少费率或尚未支持的计数类别不计入金额。 不代表订阅实际扣款或官方额度占比。"
            if mode == "api":
                price["sourceUrl"] = API_SOURCE_URL
                price["estimateBasis"] = "api_slices"
                price["note"] = "按各模型 API Standard 文本 Token 费率分别估算替代成本并相加；未知模型、缺少费率或尚未支持的计数类别不计入金额，不含工具调用等非 Token 费用。"
                if price["apiContextUncertainTokens"]:
                    price["note"] += " 上下文长度缺少足够证据的部分按短与长上下文成本的中点估算。"
                if turn.get("status") == "running":
                    price["note"] += " 本题仍在运行，这是截至目前的估算。"
                price["note"] += " 不代表实际 API 账单或订阅扣款。"
        if mode == "official":
            priced_rates = [group["standardRates"] for group in groups
                            if group["amount"] is not None and group["standardRates"] is not None]
            dates = {rate["rateDate"] for rate in priced_rates}
            if dates:
                price["rateDate"] = next(iter(dates)) if len(dates) == 1 else None
                price["historical"] = any(rate["historical"] for rate in priced_rates)
            if len(parts) > 1:
                price["note"] += " 仅按购买 credits 费率比较，不用于推算订阅内含额度或实际扣除记录。"
                if any(part["estimateBasis"] == "midpoint" for _, part in parts):
                    price["note"] += " 速度档位未确认的部分仅按 Standard 与 Fast 场景的中点估算，不含 Ultrafast。"
                if price["historical"]:
                    price["note"] += " 部分模型使用 2026-09-27 历史 credits 费率，已不在当前表中；各模型日期单独列明，不能视为现行报价。"
        return price


def credit_estimate_summary(credits, *, unpriced_tokens=0, partial=False, rates=None,
                            rate_date=None, historical=False, estimate_basis=None,
                            speed_mode="auto", speed_breakdown=None, note=None):
    """Describe an independently computed credit subtotal without currency fields."""
    status = "unavailable" if credits is None else "partial" if partial or unpriced_tokens else "estimated"
    notes = ["按各模型购买 Credits 费率估算；速度优先使用记录，其次用户确认日期，最后默认场景。不代表官方实际扣除、余额或订阅内含额度。"]
    if speed_mode == "auto":
        notes.append("速度未确认的部分仅按 Standard 与 Fast 场景的中点估算，不含 Ultrafast。")
    if unpriced_tokens:
        notes.append("缺少可靠模型、费率、速度或计数类别映射的 Token 未计入 Credits。")
    elif credits is None:
        notes.append("暂无可用的 Credits 估算。")
    if partial:
        notes.append("Credits 只覆盖已记录且可计价的部分。")
    if historical:
        notes.append("包含 2026-09-27 历史 Credits 费率，不能视为现行报价。")
    if note:
        notes.append(note)
    return {"credits": credits, "status": status, "unpricedTokens": unpriced_tokens,
            "note": " ".join(notes), "standardRates": rates, "rateDate": rate_date,
            "historical": historical, "estimateBasis": estimate_basis,
            "speedBreakdown": speed_breakdown or []}


def estimate_turn(turn: dict, settings: dict) -> dict:
    """Keep selected-currency pricing and purchased-credit estimates independent.

    Both paths use the same validated, conserved model slices. The second pass
    fixes internal currency factors at one, so invalid FX, custom prices or a
    configured credit purchase value cannot hide or change the credit count.
    It calls the internal estimator directly, never recursing through this API.
    """
    turn = turn if isinstance(turn, dict) else {}
    settings = settings if isinstance(settings, dict) else {}
    price = _estimate_turn(turn, settings)
    # Speed evidence belongs to Credits only; API/custom pricing remains independent.
    if settings.get("pricingMode", "official") != "official":
        price.pop("speedBreakdown", None)
        for row in price["models"]:
            row.pop("speedBreakdown", None)
    speed_mode = settings.get("speedMode", "auto")
    credit_price = _estimate_turn(turn, {"pricingMode": "official", "speedMode": speed_mode,
                                        "usdPerCredit": "1", "currencyPerUsd": "1",
                                        "speedOverrides": settings.get("speedOverrides", {})})
    credit_rows = {}
    for row in credit_price["models"]:
        rates = row["standardRates"]
        credit_rows[row["model"]] = credit_estimate_summary(
            row["credits"], unpriced_tokens=row["unpricedTokens"], partial=row["partial"],
            rates=rates, rate_date=rates["rateDate"] if rates else None,
            historical=bool(rates and rates["historical"]), speed_mode=speed_mode,
            speed_breakdown=row["speedBreakdown"],
            estimate_basis=credit_price["estimateBasis"] if len(credit_price["models"]) == 1 else None)
    unpriced = credit_price["unpricedTokens"]
    if not credit_rows and credit_price["credits"] is None:
        tokens = turn.get("tokens")
        total = tokens.get("total") if isinstance(tokens, dict) else None
        if type(total) is int and total >= 0:
            unpriced = total
    known_rates = [row["standardRates"] for row in credit_rows.values() if row["standardRates"]]
    if not credit_rows:
        rates = standard_rates(safe_model(turn.get("model")))
        known_rates = [rates] if rates else []
    dates = {rates["rateDate"] for rates in known_rates}
    price["creditEstimate"] = credit_estimate_summary(
        credit_price["credits"], unpriced_tokens=unpriced,
        partial=(credit_price["status"] == "partial" or bool(turn.get("readingIncomplete"))
                 or any(row["status"] != "estimated" for row in credit_rows.values())),
        rate_date=next(iter(dates)) if len(dates) == 1 else None,
        historical=any(rates["historical"] for rates in known_rates),
        estimate_basis=credit_price["estimateBasis"], speed_mode=speed_mode,
        speed_breakdown=credit_price["speedBreakdown"],
        note="本题仍在运行，这是截至目前的估算。" if turn.get("status") == "running" else None)
    for row in price["models"]:
        # Both passes share model attribution, even if one pricing mode cannot
        # price it. A missing match is never filled from another model's rate.
        row["creditEstimate"] = credit_rows.get(row["model"]) or credit_estimate_summary(
            None, unpriced_tokens=row["tokens"]["total"], speed_mode=speed_mode)
    return price
