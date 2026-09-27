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
RATE_DATE = "2026-09-27"
API_SOURCE_URL = "https://developers.openai.com/api/docs/pricing"
API_RATE_DATE = "2026-09-27"
_MILLION = Decimal(1_000_000)
_DISPLAY = Decimal("0.000001")
_MAX_COUNTER = 10**30
# Uncached input / cached input / output credits per million tokens.
RATES = {
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
# Standard processing USD per million uncached input / cached input / output.
# These are independently verified API rates, never credits times a conversion.
API_RATES = {
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
API_LONG_CONTEXT_MODELS = frozenset(API_RATES) - {"gpt-5.4-mini"}
API_LONG_CONTEXT_THRESHOLD = 272_000
FAST_MULTIPLIERS = {
    "gpt-6-sol": Decimal("2.5"),
    "gpt-6-luna": Decimal("2.5"),
    "gpt-6-astra": Decimal("2.5"),
    "gpt-5.6-sol": Decimal("2.5"),
    "gpt-5.6-terra": Decimal("2.5"),
    "gpt-5.6-luna": Decimal("2.5"),
    "gpt-5.5": Decimal("2.5"),
    "gpt-5.4": Decimal("2"),
}


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
        "label": "API 替代成本估算" if api else "按官方费率估算" if official else "自定义金额换算",
        "sourceUrl": (_api_source(model) if api else SOURCE_URL) if official else None,
        "rateDate": (API_RATE_DATE if api else RATE_DATE) if official else None,
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
                raise ValueError("记录含缓存写入 Token，当前官方费率未明确其计价方式。")

            if mode == "api":
                return _price_api(turn, settings, result, incoming, cached, outgoing, total, money_text)

            speed = settings.get("speedMode", "standard")
            fast = FAST_MULTIPLIERS.get(model)
            multiplier, multiplier_max = Decimal(1), None
            notes: list[str] = []
            if speed == "standard":
                result["estimateBasis"] = "standard"
                notes.append("按你确认的非 Fast 模式计算。")
            elif speed == "fast":
                if fast is None:
                    raise ValueError("该模型没有已核实的 Fast 倍率。")
                multiplier = fast
                result["estimateBasis"] = "fast"
                notes.append("按用户选择的 Fast 场景估算，不据此推断实际档位。")
            elif speed == "auto":
                # The official speed page documents `fast`. It does not map
                # transcript `default` or API `priority` to Standard/Fast.
                if metadata == "known" and turn.get("serviceTier") == "fast":
                    if fast is None:
                        raise ValueError("该模型没有已核实的 Fast 倍率。")
                    multiplier = fast
                    result["estimateBasis"] = "fast"
                    notes.append("按记录中的 Fast 档位估算。")
                elif fast is not None:
                    multiplier_max = fast
                    result["estimateBasis"] = "midpoint"
                    notes.append("按可用估算的中点计算。")
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
                "speedSourceUrl": SPEED_SOURCE_URL,
            }
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
                "rateDate": API_RATE_DATE if mode == "api" else RATE_DATE,
                "sourceUrl": _api_source(model) if mode == "api" else SOURCE_URL}
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
            "standardRates": standard_rates(model, mode), **{key: None for key in MONEY_FIELDS},
        })
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
        for key in MONEY_FIELDS:
            if group[key] is not None:
                group[key] = _text(group[key])
    return sorted(groups.values(), key=lambda group: (-group["tokens"]["total"], group["model"] is None, group["model"] or ""))


def estimate_turn(turn: dict, settings: dict) -> dict:
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
        if invalid:
            source.update(model=None, serviceTier=None, pricingMetadataStatus="unknown", quality="partial")
        price = _estimate_single(source, settings)
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
        key = (item["model"], item["serviceTier"], item["pricingMetadataStatus"], item["apiContext"],
               bool(item["tokens"]["cacheWriteInput"]))
        source = merged.setdefault(key, {**item, "tokens": dict.fromkeys(TOKEN_FIELDS, 0),
                                        "quality": turn.get("quality"), "status": turn.get("status")})
        for field in TOKEN_FIELDS:
            source["tokens"][field] += item["tokens"][field]
    parts = [(source, _estimate_single(source, settings, _unrounded=True)) for source in merged.values() if any(source["tokens"].values())]
    if not parts:
        price = _estimate_single(turn, settings)
        price.update(models=[], unpricedTokens=0)
        return price
    with localcontext() as context:
        context.prec = 100
        groups = _model_groups(parts, turn, mode)
        price = dict(parts[0][1]) if len(parts) == 1 else _base(turn, mode != "custom", api=mode == "api")
        price["models"] = groups
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
        return price
