"""Calendar projection of validated numeric observations, without parser changes.

The parser fingerprint intentionally excludes this presentation helper: changing
how calendar buckets are copied must not force every unchanged log to be reread.
"""
from copy import deepcopy

from usage_log import UsageLogError, _Turn, _empty_tokens, _override_windows


def reproject_usage(result, override_windows=None, *, include_segments=True):
    """Preserve observed counters and metadata while rebuilding date buckets.

    ``include_segments=False`` is for display-only summaries. The archive and
    collector must retain the original segments for validation and persistence.
    Neither mode mutates the source or returns mutable aliases into it.
    """
    projected = deepcopy({key: value for key, value in result.items() if key != "turns"})
    projected["turns"] = []
    windows = _override_windows(override_windows)
    for source in result.get("turns", []):
        turn = deepcopy(source if include_segments else
                        {key: value for key, value in source.items() if key != "usageSegments"})
        projected["turns"].append(turn)
        segments = source.get("usageSegments")
        if not isinstance(segments, list):
            continue
        rebuilt = _Turn(turn["id"], turn["threadId"], turn.get("startedAt"), override_windows=windows)
        for segment in segments:
            rebuilt.add(segment["tokens"], segment.get("timestamp"),
                        metadata={key: segment.get(key) for key in ("model", "serviceTier", "pricingMetadataStatus")},
                        api_context=segment.get("apiContext"))
        if (rebuilt.tokens or _empty_tokens()) != (turn.get("tokens") or _empty_tokens()):
            raise UsageLogError("已保存的分段用量与合计不一致。")
        # A full _Turn.export would copy its rebuilt event stream again even
        # though only these derived buckets are needed by this projection.
        turn["dailyUsage"] = {day: dict(tokens) for day, tokens in rebuilt.daily_usage.items()}
        turn["undatedTokens"] = dict(rebuilt.undated_tokens)
        turn["usageSlices"] = [
            {"day": day, "speedOverrideDay": override_day, "model": model, "serviceTier": tier,
             "pricingMetadataStatus": status, "apiContext": context, "tokens": dict(tokens)}
            for (day, override_day, model, tier, status, context, _cache_write), tokens
            in rebuilt.usage_slices.items()
        ]
    return projected
