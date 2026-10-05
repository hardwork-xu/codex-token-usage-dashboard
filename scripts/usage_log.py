"""Bounded adapter for one hook-supplied Codex JSONL usage log.

The hook's transcript path is documented, but the log format is unstable. This
adapter never discovers paths, opens conversation databases, or returns message
content. Counters describe observed usage of this one local thread, not account
quota or automatically aggregated child-agent usage.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
from datetime import datetime
from typing import Any

from conversations import source_metadata


MAX_READ_BYTES = 16 * 1024 * 1024
MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_PENDING_CONTEXTS = 128
MAX_RECORDS_PER_READ = 50_000
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_COUNTERS = (
    ("total_tokens", "total"),
    ("input_tokens", "input"),
    ("cached_input_tokens", "cachedInput"),
    ("cache_write_input_tokens", "cacheWriteInput"),
    ("output_tokens", "output"),
    ("reasoning_output_tokens", "reasoningOutput"),
)
_SOURCE_NOTE = "本地记录格式可能随 Codex 更新；每条仅统计该任务自身，子代理另列。"


class UsageLogError(ValueError):
    """A safe, content-free error describing an unreadable or untrusted log."""


def _identifier(value: Any) -> str | None:
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


def _timestamp(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) and 0 <= value <= 253402300799 else None
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        seconds = parsed.timestamp()
        return seconds if 0 <= seconds <= 253402300799 else None
    except (ValueError, OverflowError, OSError):
        return None


def _empty_tokens() -> dict[str, int]:
    return {target: 0 for _, target in _COUNTERS}


def _local_day(timestamp: float | int | None) -> str | None:
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(timestamp).date().isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def _coherent(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, int] = {}
    for source, target in _COUNTERS:
        number = value.get(source)
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            return None
        result[target] = number
    if result["total"] != result["input"] + result["output"]:
        return None
    if result["cachedInput"] > result["input"]:
        return None
    if result["reasoningOutput"] > result["output"]:
        return None
    return result


def _delta(current: dict[str, int], previous: dict[str, int]) -> dict[str, int] | None:
    delta = {key: current[key] - previous[key] for key in current}
    if any(value < 0 for value in delta.values()):
        return None
    if delta["total"] != delta["input"] + delta["output"]:
        return None
    if delta["cachedInput"] > delta["input"]:
        return None
    if delta["reasoningOutput"] > delta["output"]:
        return None
    return delta


def _bounded_last(last: dict[str, int] | None, total: dict[str, int]) -> dict[str, int] | None:
    if last is None or any(last[key] > total[key] for key in total):
        return None
    return last


class _PricingMetadata:
    """Keep bounded metadata evidence independently of token-count quality."""

    def __init__(self, *, incomplete: bool = False):
        self.first: tuple[str | None, str | None] | None = None
        self.model_changed = False
        self.tier_changed = False
        self.incomplete = incomplete

    def observe(self, model: str | None, service_tier: str | None, *, incomplete: bool = False) -> None:
        self.incomplete |= incomplete
        if self.first is None:
            self.first = (model, service_tier)
            return
        # Missing metadata is evidence of uncertainty, not a Standard default.
        self.model_changed |= self.first[0] != model
        self.tier_changed |= self.first[1] != service_tier

    def export(self) -> dict[str, Any]:
        model, service_tier = self.first if self.first is not None else (None, None)
        if self.model_changed:
            model = None
        if self.tier_changed:
            service_tier = None
        if self.model_changed or self.tier_changed:
            status = "mixed"
        elif model is not None and service_tier is not None and not self.incomplete:
            status = "known"
        else:
            status = "unknown"
        return {"model": model, "serviceTier": service_tier, "pricingMetadataStatus": status}


class _Turn:
    def __init__(self, turn_id: str, thread_id: str, started_at: float | int | None, *, started: bool = True):
        self.id = turn_id
        self.thread_id = thread_id
        self.started_at = started_at
        self.ended_at: float | int | None = None
        self.status = "running" if started else "completed"
        self.started = started
        self.tokens: dict[str, int] | None = None
        self.daily_usage: dict[str, dict[str, int]] = {}
        self.undated_tokens = _empty_tokens()
        self.usage_slices: dict[tuple, dict[str, int]] = {}
        self.reasons: list[str] = []
        self.pricing = _PricingMetadata()
        self.current_context = {"model": None, "serviceTier": None, "pricingMetadataStatus": "unknown"}
        self.interval_context = _PricingMetadata()
        self.interval_context.observe(None, None)
        if not started:
            self.mark("未记录到本轮开始。")

    def mark(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)

    def set_context(self, metadata: dict[str, Any]) -> None:
        self.current_context = dict(metadata)
        self.interval_context.observe(metadata["model"], metadata["serviceTier"],
                                      incomplete=metadata["pricingMetadataStatus"] != "known")

    def reset_interval(self) -> None:
        self.interval_context = _PricingMetadata()
        self.interval_context.observe(self.current_context["model"], self.current_context["serviceTier"],
                                      incomplete=self.current_context["pricingMetadataStatus"] != "known")

    def add(self, amount: dict[str, int], timestamp: float | int | None, *, metadata: dict[str, Any], api_context: str | None = None) -> None:
        if self.tokens is None:
            self.tokens = dict(amount)
        else:
            for key, value in amount.items():
                self.tokens[key] += value
        if any(amount.values()):
            day = _local_day(timestamp)
            bucket = self.daily_usage.setdefault(day, _empty_tokens()) if day is not None else self.undated_tokens
            for key, value in amount.items():
                bucket[key] += value
            # Freeze numeric usage under the context that covered this increment.
            # Later context, repeated snapshots and completed-turn updates cannot
            # relabel prior slices. No prompt or response bodies are retained.
            # An aggregate below the threshold bounds every constituent call.
            # A large aggregate alone never proves a long individual request.
            context = api_context or ("short" if amount["input"] <= 272_000 else "unknown")
            # These legacy API schedules apply their threshold to the full
            # session. A short call cannot prove a short session.
            if metadata["model"] in {"gpt-5.5", "gpt-5.4"} and context == "short":
                context = "unknown"
            # Cache-write mapping is not verified. Preserve the boundary so an
            # unsupported call cannot erase the priced subtotal of other calls.
            key = (day, metadata["model"], metadata["serviceTier"], metadata["pricingMetadataStatus"], context,
                   bool(amount["cacheWriteInput"]))
            sliced = self.usage_slices.setdefault(key, _empty_tokens())
            for field, value in amount.items():
                sliced[field] += value
            self.pricing.observe(metadata["model"], metadata["serviceTier"],
                                 incomplete=metadata["pricingMetadataStatus"] != "known")

    def export(self) -> dict[str, Any]:
        reasons = list(self.reasons)
        if self.tokens is None:
            reasons.append("本轮暂无可用的 token 记录。")
        note = " ".join(reasons)
        if not note:
            note = (
                "仅统计该任务自身，子代理另列；本轮仍在运行。"
                if self.status == "running"
                else "仅统计该任务自身，子代理另列。"
            )
        return {
            "id": self.id,
            "threadId": self.thread_id,
            "startedAt": self.started_at,
            "endedAt": self.ended_at,
            "status": self.status,
            "quality": "unavailable" if self.tokens is None else ("partial" if reasons else "complete"),
            "note": note,
            "tokens": dict(self.tokens) if self.tokens is not None else None,
            "dailyUsage": {day: dict(tokens) for day, tokens in self.daily_usage.items()},
            "undatedTokens": dict(self.undated_tokens),
            "usageSlices": [{"day": day, "model": model, "serviceTier": tier,
                             "pricingMetadataStatus": status, "apiContext": context, "tokens": dict(tokens)}
                            for (day, model, tier, status, context, _cache_write), tokens in self.usage_slices.items()],
            **self.pricing.export(),
        }


class _Reader:
    def __init__(self, expected_thread_id: str):
        self.thread_id = expected_thread_id
        self.verified = False
        self.source_metadata = {}
        self.turns: dict[str, _Turn] = {}
        self.active: _Turn | None = None
        self.previous_total: dict[str, int] | None = None
        self.baseline_valid = False
        self.warnings: list[str] = []
        self.pending_contexts: dict[str, dict[str, Any]] = {}
        self.pending_context_overflow = False
        self.fork_history_start: int | None = None
        self.fork_last_ordinal = 0
        self.fork_owned_history = False
        self.fork_expected_ancestor: str | None = None
        self.fork_ancestors: set[str] = set()

    def configure_fork_history(self, record: dict[str, Any], payload: dict[str, Any]) -> None:
        """Honor only an explicit, identity-bound projected-history boundary.

        Codex's SessionMeta defines subagent_history_start_ordinal as the first
        ordinal owned by the child. Earlier records are inherited model context,
        including ancestor session headers and token events. Use record.ordinal,
        never raw line numbers, so blank or unreadable lines cannot move it.
        """
        boundary = payload.get("subagent_history_start_ordinal")
        if boundary is None:
            return
        parent = _identifier(payload.get("parent_thread_id"))
        forked_from = _identifier(payload.get("forked_from_id"))
        source = source_metadata(payload)
        if (type(boundary) is not int or not 0 <= boundary < 2**64
                or payload.get("history_mode") != "paginated"
                or type(record.get("ordinal")) is not int or record["ordinal"] != 0
                or parent is None or forked_from != parent or parent == self.thread_id
                or source.get("sourceType") != "subagent"
                or source.get("parentThreadId") not in (None, parent)):
            raise UsageLogError("子任务继承历史的身份或边界无效，无法安全统计。")
        self.fork_history_start = boundary
        self.fork_expected_ancestor = parent

    def inherited_record(self, record: dict[str, Any]) -> bool:
        """Validate fork ordinals and exclude the declared ancestor prefix."""
        if self.fork_history_start is None:
            return False
        ordinal = record.get("ordinal")
        if type(ordinal) is not int or not self.fork_last_ordinal < ordinal < 2**64:
            raise UsageLogError("子任务记录缺少有效的递增序号，无法安全区分继承用量。")
        self.fork_last_ordinal = ordinal
        if ordinal >= self.fork_history_start:
            self.fork_owned_history = True
            return False
        if record.get("type") == "session_meta":
            payload = record.get("payload")
            ancestor = _identifier(payload.get("id")) if isinstance(payload, dict) else None
            if ancestor is None or (ancestor != self.fork_expected_ancestor and ancestor not in self.fork_ancestors):
                raise UsageLogError("子任务继承历史包含未声明的任务身份。")
            if ancestor == self.fork_expected_ancestor:
                if len(self.fork_ancestors) >= 128:
                    raise UsageLogError("子任务继承层级过多，无法安全统计。")
                self.fork_ancestors.add(ancestor)
                next_ancestor = _identifier(payload.get("forked_from_id"))
                if next_ancestor == self.thread_id or next_ancestor in self.fork_ancestors:
                    raise UsageLogError("子任务继承历史的祖先关系存在循环。")
                self.fork_expected_ancestor = next_ancestor
        # Older paginated forks copy context records without an ancestor header.
        # The child's own verified metadata supplies the explicit boundary and
        # parent identity. If copied headers exist, their ancestry is still
        # checked above; their absence never turns inherited counters into usage.
        return True

    def warn(self, warning: str) -> None:
        if warning not in self.warnings:
            self.warnings.append(warning)

    def gap(self, reason: str) -> None:
        self.baseline_valid = False
        self.warn(reason)
        if self.active is not None:
            self.active.mark(reason)
            self.active.set_context({"model": None, "serviceTier": None, "pricingMetadataStatus": "unknown"})

    def handle(self, record: Any) -> None:
        if not isinstance(record, dict):
            self.gap("已跳过无法识别的记录。")
            return
        if self.verified and self.inherited_record(record):
            return
        kind = record.get("type")
        payload = record.get("payload")
        if kind == "session_meta":
            if not isinstance(payload, dict) or payload.get("id") != self.thread_id:
                raise UsageLogError("用量记录与指定任务不匹配。")
            if not self.verified:
                self.configure_fork_history(record, payload)
            self.verified = True
            self.source_metadata = source_metadata(payload)
            return
        if kind == "turn_context":
            if not self.verified:
                raise UsageLogError("任务上下文之前缺少任务身份记录。")
            if isinstance(payload, dict):
                self.context(payload)
            return
        # Ignore all messages, reasoning, tool bodies, and unknown record types.
        if kind == "compacted":
            if self.verified:
                self.gap("上下文压缩改变了统计基线，用量可能不完整。")
            return
        if kind != "event_msg" or not isinstance(payload, dict):
            return
        event = payload.get("type")
        if event not in {"task_started", "task_complete", "token_count", "context_compacted", "thread_compacted", "turn_aborted"}:
            return
        if not self.verified:
            raise UsageLogError("用量事件之前缺少任务身份记录。")
        timestamp = _timestamp(record.get("timestamp"))
        if event in {"context_compacted", "thread_compacted"}:
            self.gap("上下文压缩改变了统计基线，用量可能不完整。")
        elif event == "task_started":
            self.start(payload, timestamp)
        elif event in {"task_complete", "turn_aborted"}:
            self.finish(payload, timestamp, interrupted=event == "turn_aborted")
        elif event == "token_count":
            self.usage(payload, timestamp)

    def context(self, payload: dict[str, Any]) -> None:
        # Deliberately select only these three context fields. In particular,
        # never use prompts, instructions, cwd, or inferred account defaults.
        turn_id = _identifier(payload.get("turn_id"))
        if turn_id is None:
            return
        model = _identifier(payload.get("model"))
        service_tier = _identifier(payload.get("service_tier"))
        metadata = {"model": model, "serviceTier": service_tier,
                    "pricingMetadataStatus": "known" if model is not None and service_tier is not None
                    and not self.pending_context_overflow else "unknown"}
        turn = self.turns.get(turn_id)
        if turn is not None:
            if turn.status == "running":
                turn.set_context(metadata)
            return
        if turn_id not in self.pending_contexts:
            if len(self.pending_contexts) >= MAX_PENDING_CONTEXTS:
                self.pending_contexts.pop(next(iter(self.pending_contexts)))
                self.pending_context_overflow = True
                # An evicted ID could appear again. Preserve bounded memory and
                # avoid later claiming complete metadata after losing evidence.
                for pending in self.pending_contexts.values():
                    pending["pricingMetadataStatus"] = "unknown"
                self.warn("待归属的模型记录过多，后续计价信息可能不完整。")
        if self.pending_context_overflow:
            metadata["pricingMetadataStatus"] = "unknown"
        # Context changes without accepted usage do not create model slices.
        self.pending_contexts[turn_id] = metadata

    def attach_context(self, turn: _Turn) -> None:
        metadata = self.pending_contexts.pop(turn.id, None)
        if metadata is not None:
            turn.set_context(metadata)
            # This exact-turn context predates its start, so it already covers
            # the new turn's interval. An initial unknown placeholder is not
            # evidence that this interval crossed a model boundary.
            turn.reset_interval()

    def add_usage(self, turn: _Turn, amount: dict[str, int], timestamp: float | int | None,
                  last: dict[str, int] | None, *, cumulative_delta: bool = False) -> None:
        """Attribute only the evidence available for an accepted increment.

        A delta crossing context changes may contain multiple calls. A coherent
        last-call subset belongs to the current context; any coherent remainder
        keeps only metadata unchanged across the interval. Without such a split,
        the whole delta retains that uncertain interval attribution. These are
        log-context labels, not independently verified provider routing data.
        """
        interval = turn.interval_context
        changed = cumulative_delta and (interval.model_changed or interval.tier_changed)
        metadata = interval.export() if changed else turn.current_context
        if changed:
            metadata["pricingMetadataStatus"] = "unknown"
        remainder = _delta(amount, last) if last is not None else None
        if remainder is not None:
            # last_token_usage is a single-call observation. Keep its context
            # band even when a preceding delta contains multiple missing calls.
            if any(remainder.values()):
                turn.add(remainder, timestamp, metadata=metadata)
            turn.add(last, timestamp, metadata=turn.current_context,
                     api_context="long" if last["input"] > 272_000 else "short")
        else:
            turn.add(amount, timestamp, metadata=metadata,
                     api_context="unknown" if last is not None else None)
        if any(amount.values()):
            turn.reset_interval()

    def start(self, payload: dict[str, Any], timestamp: float | int | None) -> None:
        turn_id = _identifier(payload.get("turn_id"))
        if turn_id is None:
            self.gap("已跳过缺少有效轮次编号的开始事件。")
            if self.active is not None:
                self.active.status = "interrupted"
                self.active.ended_at = timestamp
            self.active = None
            return
        if turn_id in self.turns:
            # Replayed starts never clear the baseline or reopen completed turns.
            return
        if self.active is not None:
            self.active.mark("记录到下一轮开始，但未记录到本轮结束。")
            self.active.status = "interrupted"
            self.active.ended_at = timestamp
        self.active = _Turn(turn_id, self.thread_id, timestamp)
        self.attach_context(self.active)
        self.turns[turn_id] = self.active

    def finish(self, payload: dict[str, Any], timestamp: float | int | None, *, interrupted: bool) -> None:
        turn_id = _identifier(payload.get("turn_id"))
        if turn_id is None:
            self.gap("结束事件缺少有效轮次编号，已暂停归属统计。")
            # Its identity is unknown, so further unlabelled snapshots cannot
            # safely be assigned to the previously active turn.
            if self.active is not None:
                self.active.status = "interrupted"
                self.active.ended_at = timestamp
            self.active = None
            return
        turn = self.turns.get(turn_id)
        if turn is None:
            turn = _Turn(turn_id, self.thread_id, None, started=False)
            self.attach_context(turn)
            self.turns[turn_id] = turn
        elif turn.status != "running":
            return
        turn.status = "interrupted" if interrupted else "completed"
        turn.ended_at = timestamp
        if self.active is turn:
            self.active = None

    def usage(self, payload: dict[str, Any], timestamp: float | int | None) -> None:
        info = payload.get("info")
        if info is None:
            # Quota-only notifications can carry null info. It is not zero usage.
            return
        if not isinstance(info, dict):
            self.gap("已跳过无效 token 记录，用量可能不完整。")
            return
        total = _coherent(info.get("total_token_usage"))
        last = _coherent(info.get("last_token_usage"))
        if total is None:
            self.gap("已跳过无效 token 记录，用量可能不完整。")
            return

        turn = self.active
        explicit_turn = payload.get("turn_id")
        explicit_thread = payload.get("thread_id")
        if explicit_thread is not None and explicit_thread != self.thread_id:
            self.warn("已排除其他任务的 token 记录。")
            if turn is not None:
                turn.mark("已排除其他任务的 token 记录。")
            # Another thread has its own cumulative baseline. It must never
            # replace this thread's baseline, even if its counters are valid.
            return
        if explicit_turn is not None and (turn is None or explicit_turn != turn.id):
            if turn is not None:
                turn.mark("已排除其他轮次的 token 记录。")
            turn = None

        repeated = total == self.previous_total
        if turn is None:
            self.warn("已排除无法归属到进行中轮次的用量。")
        elif repeated:
            if self.baseline_valid:
                self.add_usage(turn, {key: 0 for key in total}, timestamp, last)
            else:
                turn.mark("缺少完整的起始 token 基线。")
        elif self.baseline_valid and self.previous_total is not None:
            amount = _delta(total, self.previous_total)
            if amount is not None:
                self.add_usage(turn, amount, timestamp, last, cumulative_delta=True)
            else:
                turn.mark("token 计数重置或变化异常，用量可能不完整。")
                self.warn("检测到 token 计数重置或差值异常。")
                fallback = _bounded_last(last, total)
                if fallback is not None:
                    self.add_usage(turn, fallback, timestamp, last)
        else:
            fallback = _bounded_last(last, total)
            if fallback is not None and total == fallback and turn.started:
                self.add_usage(turn, fallback, timestamp, last)
            else:
                turn.mark("缺少完整起始基线，仅统计已观察到的增量。")
                if fallback is not None:
                    self.add_usage(turn, fallback, timestamp, last)
        self.previous_total = total
        self.baseline_valid = True

    def result(self) -> dict[str, Any]:
        if not self.verified:
            raise UsageLogError("用量记录中缺少匹配的任务身份。")
        warnings = list(self.warnings)
        if self.fork_history_start is not None and not self.fork_owned_history:
            warnings.append("已核实子任务身份，尚未读到自身记录；继承的父任务用量不计入。")
        return {
            "threadId": self.thread_id,
            "identityVerified": self.verified,
            "conversationMetadata": dict(self.source_metadata),
            "turns": [turn.export() for turn in self.turns.values()],
            "warnings": warnings,
            "sourceNote": _SOURCE_NOTE,
        }


def _ordinary_leaf(details):
    return (stat.S_ISREG(details.st_mode) and
            not getattr(details, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


class UsageLogReader:
    """Incrementally read one exact path, retaining only sanitized statistics.

    Each call reads at most ``byte_budget`` bytes, including bounded integrity
    checks, and processes at most 50,000 records. No raw JSON or unfinished line
    is retained between calls. An unfinished line is reread from its offset once
    the file changes. A complete JSON value without a newline is not committed.

    File identity, size and bounded prefix/cursor digests detect replacement and
    common in-place rewrites, including truncate-and-regrow. Arbitrary edits in
    an already-read middle region cannot be detected without rereading history;
    the documented local adapter expects an append-only active log.
    """

    def __init__(self, expected_thread_id: str, *, byte_budget: int = MAX_READ_BYTES):
        if _identifier(expected_thread_id) is None:
            raise UsageLogError("需要有效的任务编号。")
        self._thread_id = expected_thread_id
        self._line_limit = MAX_LINE_BYTES
        self._anchor_bytes = min(4096, max(1, self._line_limit // 8))
        minimum = self._line_limit + 1 + 2 * self._anchor_bytes
        if isinstance(byte_budget, bool) or not isinstance(byte_budget, int) or not minimum <= byte_budget <= MAX_READ_BYTES:
            raise ValueError("单次读取预算必须足够容纳一条记录及完整性检查，且不能超过 16 MiB。")
        self._byte_budget = byte_budget
        self._lock = threading.Lock()
        self._reset(None, None)

    def _reset(self, path, identity):
        self._path = path
        self._identity = identity
        self._reader = _Reader(self._thread_id)
        self._offset = 0
        self._observed_size = None
        self._mtime_ns = None
        self._prefix = None
        self._tail = None
        self._skipping = False
        self._waiting_at_end = False
        self._failure = None

    def _remember_tail(self, raw):
        tail = raw[-self._anchor_bytes:]
        self._tail = (self._offset - len(tail), len(tail), hashlib.sha256(tail).digest())

    def _poison(self, message):
        self._failure = message
        # Never return cached totals after discovering a foreign session later.
        self._reader = _Reader(self._thread_id)
        raise UsageLogError(message)

    def _result(self, file_bytes):
        complete = self._offset == file_bytes and not self._skipping and not self._waiting_at_end
        if complete and not self._reader.verified:
            # A newly-created file may receive its first metadata on the next
            # append. Absence is not affirmative evidence of a foreign session.
            raise UsageLogError("用量记录中缺少匹配的任务身份。")
        result = (self._reader.result() if self._reader.verified else
                  {"threadId": self._thread_id, "identityVerified": False, "conversationMetadata": {},
                   "turns": [], "warnings": [], "sourceNote": _SOURCE_NOTE})
        result = deepcopy(result)
        result["reading"] = {"complete": complete, "bytesRead": self._offset, "fileBytes": file_bytes}
        if not complete:
            note = ("末条记录尚未写完，请稍后刷新。" if self._waiting_at_end else
                    "正在分批读取历史用量，当前统计尚不完整。")
            result["warnings"].append(note)
            for turn in result["turns"]:
                turn["readingIncomplete"] = True
                if turn["tokens"] is not None:
                    turn["quality"] = "partial"
                turn["note"] = note + " " + turn["note"]
        return result

    def read(self, path: str | os.PathLike[str], *, time_budget: float = 1.0) -> dict[str, Any]:
        """Apply a soft parsing budget; one record may finish after the deadline."""
        if (isinstance(time_budget, bool) or not isinstance(time_budget, (int, float)) or
                not 0 < time_budget <= 1 or not math.isfinite(time_budget)):
            raise ValueError("单次解析时间预算必须是大于零且不超过 1 秒的有限数值。")
        with self._lock:
            return self._read(path, time_budget)

    def _read(self, path, time_budget):
        try:
            file_path = Path(path).expanduser().absolute()
        except (TypeError, ValueError):
            raise UsageLogError("需要用量记录的文件路径。") from None
        if file_path.suffix.lower() != ".jsonl":
            raise UsageLogError("用量记录必须是 JSONL 文件。")
        path_key = os.path.normcase(str(file_path))
        if path_key != self._path:
            self._reset(path_key, None)
        flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) |
                 getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
        try:
            original = file_path.lstat()
            if not _ordinary_leaf(original):
                raise UsageLogError("用量记录路径必须指向普通文件，不能是链接。")
            descriptor = os.open(file_path, flags)
        except (OSError, ValueError):
            raise UsageLogError("无法打开指定的用量记录文件。") from None
        try:
            with os.fdopen(descriptor, "rb") as stream:
                details = os.fstat(stream.fileno())
                current = file_path.lstat()
                identity = (details.st_dev, details.st_ino)
                if (not _ordinary_leaf(details) or not _ordinary_leaf(current) or
                        identity != (original.st_dev, original.st_ino) or
                        identity != (current.st_dev, current.st_ino)):
                    raise UsageLogError("用量记录路径必须指向普通文件。")
                size, stamp = details.st_size, details.st_mtime_ns
                if (identity != self._identity or
                        self._observed_size is not None and
                        (size < self._observed_size or
                         size == self._observed_size and stamp != self._mtime_ns and not self._failure)):
                    self._reset(path_key, identity)

                unchanged = size == self._observed_size and stamp == self._mtime_ns
                if unchanged and self._failure:
                    raise UsageLogError(self._failure)
                if unchanged and (self._offset == size or self._waiting_at_end):
                    return self._result(size)

                spent = 0
                # Store only digests, never the checked raw prefix/cursor bytes.
                prefix = stream.read(min(size, self._anchor_bytes))
                spent += len(prefix)
                rewritten = (self._prefix is not None and
                             hashlib.sha256(prefix[:self._prefix[0]]).digest() != self._prefix[1])
                if self._tail is not None and not rewritten:
                    start, length, digest = self._tail
                    stream.seek(start)
                    anchor = stream.read(length)
                    spent += len(anchor)
                    rewritten = hashlib.sha256(anchor).digest() != digest
                if rewritten:
                    self._reset(path_key, identity)
                self._prefix = (len(prefix), hashlib.sha256(prefix).digest())
                self._observed_size, self._mtime_ns = size, stamp
                if self._failure:
                    raise UsageLogError(self._failure)

                stream.seek(self._offset)
                self._waiting_at_end = False
                deadline = time.monotonic() + time_budget
                for record_index in range(MAX_RECORDS_PER_READ):
                    if (self._offset >= size or spent >= self._byte_budget or
                            record_index > 0 and time.monotonic() >= deadline):
                        break
                    line_start = self._offset
                    limit = min(self._byte_budget - spent, size - self._offset,
                                64 * 1024 if self._skipping else self._line_limit + 1)
                    raw = stream.readline(limit)
                    spent += len(raw)
                    if not raw:
                        self._reset(path_key, None)
                        raise UsageLogError("用量记录在读取时发生变化，请稍后刷新。")
                    if self._skipping:
                        self._offset += len(raw)
                        self._remember_tail(raw)
                        self._skipping = not raw.endswith(b"\n")
                        continue
                    if len(raw) > self._line_limit:
                        self._reader.gap("已跳过过大的单条记录，用量可能不完整。")
                        self._offset += len(raw)
                        self._remember_tail(raw)
                        self._skipping = not raw.endswith(b"\n")
                        continue
                    if not raw.endswith(b"\n"):
                        # Neither parse nor cache an unfinished line. On a budget
                        # boundary it will fit in the next call's fresh budget.
                        self._waiting_at_end = line_start + len(raw) >= size
                        break
                    self._offset += len(raw)
                    self._remember_tail(raw)
                    if not raw.strip():
                        continue
                    try:
                        record = json.loads(raw)
                    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
                        self._reader.gap("已跳过格式异常的记录，用量可能不完整。")
                        continue
                    try:
                        self._reader.handle(record)
                    except UsageLogError as exc:
                        self._poison(str(exc))
                if self._skipping and self._offset == size:
                    self._waiting_at_end = True
                final = file_path.lstat()
                final_handle = os.fstat(stream.fileno())
                if (not _ordinary_leaf(final) or (final.st_dev, final.st_ino) != identity or
                        final_handle.st_size < size or
                        final_handle.st_size == size and final_handle.st_mtime_ns != stamp):
                    self._reset(path_key, None)
                    raise UsageLogError("用量记录在读取时发生变化，请稍后刷新。")
                return self._result(size)
        except OSError:
            raise UsageLogError("无法读取指定的用量记录文件。") from None


def read_usage_log(path: str | os.PathLike[str], expected_thread_id: str) -> dict[str, Any]:
    """One bounded compatibility read; reuse UsageLogReader to finish backfill."""
    return UsageLogReader(expected_thread_id).read(path)


__all__ = ["UsageLogError", "UsageLogReader", "read_usage_log"]
