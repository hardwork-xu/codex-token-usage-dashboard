#!/usr/bin/env python3
"""Local-only Codex usage meter. Standard-library runtime; no model calls."""
from __future__ import annotations

import argparse
import contextlib
from copy import deepcopy
import decimal
import json
import math
import os
from pathlib import Path
import secrets
import socket
import socketserver
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request, error, parse

from usage_log import read_usage_log, UsageLogReader
from pricing import estimate_turn
from official_credits import (account_scope, credits_from_response, credit_view,
                              record_balance, fetch_thread_usage, thread_usage_view)
from periods import summarize_periods
from conversations import fetch_conversation_titles, normalize_title, display_ids
from platform_support import default_data_dir, codex_command, file_lock, is_windows, subprocess_options
from service_lifecycle import read_service_mode, activate_launchd_socket, adopt_http_socket, SOCKET_NAME

ROOT = Path(__file__).resolve().parent.parent
EXCHANGE_RATES = {"CNY": "6.70842351", "USD": "1", "HKD": "7.84339018"}
CURRENCIES = {"CNY": ("人民币", "¥"), "USD": ("美元", "$"), "HKD": ("港元", "HK$")}
FX_REFERENCE = {"sourceUrl": "https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html",
                "date": "2026-09-14", "label": "欧洲央行参考汇率"}
DEFAULT_SETTINGS = {"currencyName": "美元", "currencySymbol": "$", "ratePerMillion": None,
                    "pricingMode": "api", "usdPerCredit": "0.04", "currencyPerUsd": "1", "speedMode": "standard",
                    "currencyCode": "USD", "exchangeRates": EXCHANGE_RATES, "subscriptionRenewalDay": None}
QUOTA_STALE_SECONDS = 120
QUOTA_REFRESH_COOLDOWN = 30
EVENTS = {"SessionStart", "UserPromptSubmit", "Stop", "Interrupt", "SubagentStop", "SubagentStart"}


def data_dir(value=None):
    base = value or os.environ.get("PLUGIN_DATA") or os.environ.get("METER_DATA_DIR")
    return Path(base).expanduser().resolve() if base else default_data_dir()


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out, ensure_ascii=False, indent=2)
            out.write("\n")
        # Windows may briefly deny replacement while a reader has the old file open.
        for attempt in range(11):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if not is_windows() or attempt == 10:
                    raise
                time.sleep(0.05)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


@contextlib.contextmanager
def locked(folder, name="registry"):
    with file_lock(folder / (name + ".lock")):
        yield


def register(folder, path, thread_id):
    """Only the exact caller-provided log. No discovery or recursive scanning."""
    path = Path(path).expanduser().absolute()
    result = read_usage_log(path, thread_id)
    if result.get("identityVerified") is not True:
        raise ValueError("尚未核实这份记录的任务编号，请稍后重试")
    with locked(folder):
        records = read_json(folder / "registry.json", {})
        if not isinstance(records, dict):
            raise ValueError("本地登记记录格式无效，请检查数据目录")
        previous = records.get(thread_id)
        previous = previous if isinstance(previous, dict) else {}
        records[thread_id] = {**previous, "path": str(path), "registeredAt": previous.get("registeredAt", time.time())}
        write_json(folder / "registry.json", records)
    return {"threadId": thread_id, "turnCount": len(result["turns"])}


def set_conversation_label(folder, value):
    if not isinstance(value, dict) or set(value) != {"threadId", "title"}:
        raise ValueError("对话备注字段无效")
    thread_id, raw_title = value["threadId"], value["title"]
    if not isinstance(thread_id, str) or not isinstance(raw_title, str) or len(raw_title) > 240:
        raise ValueError("对话编号或备注无效")
    title = normalize_title(raw_title)
    if raw_title.strip() and not title:
        raise ValueError("请输入有效的对话备注")
    with locked(folder):
        records = read_json(folder / "registry.json", {})
        if not isinstance(records, dict) or thread_id not in records or not isinstance(records[thread_id], dict):
            raise ValueError("该对话尚未登记")
        if title:
            records[thread_id]["alias"] = title
        else:
            records[thread_id].pop("alias", None)
        write_json(folder / "registry.json", records)
    return {"ok": True}


def conversation_catalog(records, turns, titles, metadata=None):
    """Attach names, collision-free short IDs and per-conversation turn numbers."""
    ids = [key for key, value in records.items() if isinstance(key, str) and isinstance(value, dict)]
    ids.sort(key=lambda key: (records[key].get("registeredAt")
                             if isinstance(records[key].get("registeredAt"), (int, float)) else 0, key))
    tags = display_ids(ids)
    metadata = metadata or {}
    names = {}
    for thread_id in ids:
        alias = normalize_title(records[thread_id].get("alias"))
        official = normalize_title(titles.get(thread_id))
        info = metadata.get(thread_id, {})
        child = info.get("sourceType") == "subagent"
        label = normalize_title(info.get("agentLabel"))
        fallback = ("子任务 · " + (label or tags[thread_id])) if child else "未命名任务 · " + tags[thread_id]
        names[thread_id] = (alias or official or fallback, "custom" if alias else "official" if official else "fallback")
    catalog = []
    for thread_id in ids:
        title, title_source = names[thread_id]
        info = metadata.get(thread_id, {})
        source_type = info.get("sourceType", "unknown")
        parent = info.get("parentThreadId") if source_type == "subagent" else None
        if parent == thread_id:
            parent = None
        items = sorted((turn for turn in turns if turn.get("threadId") == thread_id),
                       key=lambda item: (item.get("startedAt") or 0, item.get("id") or ""))
        for number, turn in enumerate(items, 1):
            turn.update(conversationTitle=title, conversationDisplayId=tags[thread_id], turnNumber=number)
        catalog.append({"id": thread_id, "title": title, "displayId": tags[thread_id],
                        "titleSource": title_source, "sourceType": source_type,
                        "parentThreadId": parent, "parentTitle": names.get(parent, (None,))[0],
                        "parentDisplayId": tags.get(parent) if parent else None,
                        "agentLabel": info.get("agentLabel"),
                        "activeTurnCount": sum(turn.get("status") == "running" for turn in items),
                        "turnCount": len(items),
                        "lastActivityAt": max((turn.get("endedAt") or turn.get("startedAt") or 0 for turn in items), default=0)})
    catalog.sort(key=lambda row: (bool(row["activeTurnCount"]), row["lastActivityAt"]), reverse=True)
    return catalog


def codex_binary():
    command = codex_command()
    if len(command) != 1:
        raise RuntimeError("此 Codex 安装需要通过完整命令启动")
    return command[0]


def fetch_quota():
    """Use documented quota RPC over bounded, cross-platform UTF-8 pipes."""
    from rpc_transport import JsonRpcProcess
    try:
        with JsonRpcProcess([*codex_command(), "app-server", "--stdio"], timeout=20) as rpc:
            rpc.send({"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "codex_usage_meter", "version": "0.10.1"}}})
            initialized = False
            for response in rpc.responses():
                request_id = response.get("id")
                if type(request_id) is not int:
                    continue
                if not initialized and request_id == 1:
                    if "error" in response or not isinstance(response.get("result"), dict):
                        raise RuntimeError("Codex 初始化失败，请检查登录状态")
                    initialized = True
                    rpc.send({"method": "initialized"})
                    rpc.send({"id": 2, "method": "account/rateLimits/read"})
                elif initialized and request_id == 2:
                    if "error" in response:
                        detail = response.get("error")
                        message = str(detail.get("message", ""))[:1000].lower() if isinstance(detail, dict) else ""
                        if any(term in message for term in ("error sending request", "connection", "timed out", "timeout")):
                            raise RuntimeError("官方额度连接失败，请检查本机网络或代理；当前保留上次成功的额度")
                        raise RuntimeError("Codex 官方额度查询未成功，请稍后重试")
                    return normalize_quota(response.get("result", {}))
    except TimeoutError:
        raise RuntimeError("额度查询超时，请稍后重试") from None
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("Codex 用量连接暂不可用，请检查本地运行环境") from None
    raise RuntimeError("Codex 用量连接已关闭")


def normalize_quota(value):
    if not isinstance(value, dict):
        raise RuntimeError("Codex 未返回有效的额度数据")
    buckets = value.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        old = value.get("rateLimits")
        buckets = {old.get("limitId") or "codex": old} if isinstance(old, dict) else {}
    output = []
    for key, bucket in sorted(buckets.items(), key=lambda item: (item[0] != "codex", item[0])):
        if not isinstance(bucket, dict):
            continue
        windows = []
        for name in ("primary", "secondary"):
            window = bucket.get(name)
            if not isinstance(window, dict):
                continue
            used = window.get("usedPercent")
            if isinstance(used, bool) or not isinstance(used, (int, float)) or not 0 <= used <= 100:
                continue
            windows.append({"windowMinutes": window.get("windowDurationMins"),
                            "usedPercent": used, "remainingPercent": 100 - used,
                            "resetsAt": window.get("resetsAt")})
        output.append({"id": key, "label": bucket.get("limitName") or ("Codex 主额度" if key == "codex" else key), "windows": windows})
    return {"updatedAt": time.time(), "error": None, "buckets": output,
            "credits": credits_from_response(value),
            "accountScope": account_scope(value.get("accountId"))}


def quota_view(value, *, now, last_attempt=None, refreshing=False, validated=True):
    """Annotate last-good observations without presenting stale quota as current."""
    result = deepcopy(value) if isinstance(value, dict) else {"updatedAt": None, "error": "额度缓存无效", "buckets": []}
    def timestamp(value):
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and 0 < value <= 253402300799 and math.isfinite(value))
    updated = result.get("updatedAt")
    buckets = result.get("buckets")
    # A corrupt cache must not leak NaN or malformed rows into the JSON API.
    clean_buckets = []
    for bucket in buckets if isinstance(buckets, list) else []:
        if not isinstance(bucket, dict):
            continue
        clean_windows = []
        for window in bucket.get("windows") if isinstance(bucket.get("windows"), list) else []:
            remaining = window.get("remainingPercent") if isinstance(window, dict) else None
            if isinstance(remaining, bool) or not isinstance(remaining, (int, float)) or not 0 <= remaining <= 100:
                continue
            minutes = window.get("windowMinutes")
            clean_windows.append({"remainingPercent": remaining, "usedPercent": 100 - remaining,
                                  "windowMinutes": minutes if timestamp(minutes) else None,
                                  "resetsAt": window.get("resetsAt") if timestamp(window.get("resetsAt")) else None})
        clean_buckets.append({"id": str(bucket.get("id", "")),
                              "label": str(bucket.get("label", "额度")), "windows": clean_windows})
    result = {"updatedAt": updated if timestamp(updated) else None,
              "error": str(result["error"]) if result.get("error") else None, "buckets": clean_buckets}
    windows = [window for bucket in clean_buckets for window in bucket["windows"]]
    expires = updated + QUOTA_STALE_SECONDS if timestamp(updated) else None
    if expires is not None:
        resets = [window["resetsAt"] for window in windows if timestamp(window.get("resetsAt"))]
        if resets:
            expires = min(expires, *resets)
    status = "unavailable" if not windows or expires is None else (
        "stale" if not validated or result.get("error") or updated > now + 5 or now >= expires else "fresh")
    result.update(status=status, expiresAt=expires, staleAfterSeconds=QUOTA_STALE_SECONDS,
                  lastAttemptAt=last_attempt if timestamp(last_attempt) else None,
                  refreshing=bool(refreshing))
    return result


def positive_conversion(raw):
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)) or len(str(raw)) > 30:
        raise ValueError("Credits 单价或货币换算比例无效")
    try:
        rate = decimal.Decimal(str(raw))
    except decimal.InvalidOperation as exc:
        raise ValueError("Credits 单价或货币换算比例无效") from exc
    if not rate.is_finite() or rate < decimal.Decimal("0.000000000001") or rate > 1_000_000_000:
        raise ValueError("Credits 单价和货币比例必须大于零，范围为 0.000000000001 到 10 亿")
    return str(rate)


def validate_settings(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULT_SETTINGS):
        raise ValueError("设置字段无效")
    out = {}
    renewal_day = value.get("subscriptionRenewalDay")
    if renewal_day is not None and (type(renewal_day) is not int or not 1 <= renewal_day <= 31):
        raise ValueError("订阅续费日应为 1 到 31 的整数，或留空")
    out["subscriptionRenewalDay"] = renewal_day
    for key, choices in (("pricingMode", ("api", "official", "custom")), ("speedMode", ("auto", "standard", "fast", "ultrafast"))):
        selected = value.get(key, DEFAULT_SETTINGS[key])
        if selected not in choices:
            raise ValueError("计价方式或速度选项无效")
        out[key] = selected
    for key, limit in (("currencyName", 24), ("currencySymbol", 8)):
        text = value.get(key, DEFAULT_SETTINGS[key])
        if not isinstance(text, str) or not text.strip() or len(text) > limit or any(ord(c) < 32 for c in text):
            raise ValueError("货币名称或符号无效")
        out[key] = text.strip()
    for key in ("usdPerCredit", "currencyPerUsd"):
        out[key] = positive_conversion(value.get(key, DEFAULT_SETTINGS[key]))
    raw_rates = value.get("exchangeRates", EXCHANGE_RATES)
    if not isinstance(raw_rates, dict) or set(raw_rates) != set(EXCHANGE_RATES):
        raise ValueError("需要人民币、美元和港元三个汇率")
    out["exchangeRates"] = {key: positive_conversion(raw_rates[key]) for key in EXCHANGE_RATES}
    if decimal.Decimal(out["exchangeRates"]["USD"]) != 1:
        raise ValueError("美元基准汇率必须为 1")
    out["exchangeRates"]["USD"] = "1"
    code = value.get("currencyCode")
    if code is None:
        code = "USD" if all(out[key] == DEFAULT_SETTINGS[key] for key in ("currencyName", "currencySymbol", "currencyPerUsd")) else "CUSTOM"
    if not isinstance(code, str) or code not in (*CURRENCIES, "CUSTOM"):
        raise ValueError("货币选项无效")
    # Legacy total-token pricing is denominated in the user's chosen units.
    # Keep that mode custom so a preset cannot silently reinterpret its rate.
    if out["pricingMode"] == "custom":
        code = "CUSTOM"
    out["currencyCode"] = code
    if code in CURRENCIES:
        out["currencyName"], out["currencySymbol"] = CURRENCIES[code]
        out["currencyPerUsd"] = out["exchangeRates"][code]
    raw = value.get("ratePerMillion")
    if raw is None or raw == "":
        out["ratePerMillion"] = None
    else:
        if isinstance(raw, bool) or len(str(raw)) > 30:
            raise ValueError("换算单价无效")
        try:
            rate = decimal.Decimal(str(raw))
        except decimal.InvalidOperation as exc:
            raise ValueError("换算单价无效") from exc
        if not rate.is_finite() or rate < 0 or rate > 1_000_000_000:
            raise ValueError("换算单价应在 0 到 10 亿之间")
        out["ratePerMillion"] = str(rate)
    return out


class Meter:
    def __init__(self, folder):
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cache = {}
        self.reader_incomplete = {}
        self.snapshot_lock = threading.Lock()
        self.quota = read_json(folder / "quota.json", {"updatedAt": None, "error": "尚未查询额度", "buckets": []})
        if not isinstance(self.quota, dict):
            self.quota = {"updatedAt": None, "error": "额度缓存无效", "buckets": []}
        self.quota_verified = False
        self.credit_ledger = read_json(folder / "credit-balances.json", {})
        self.official_usage = {}
        self.official_usage_attempts = {}
        self.official_usage_lock = threading.Lock()
        self.official_usage_thread = None
        self.refresh_lock = threading.Lock()
        self.last_attempt = 0
        self.last_client = time.time()
        self.csrf = secrets.token_urlsafe(32)
        self.titles = read_json(folder / "titles.json", {})
        if not isinstance(self.titles, dict):
            self.titles = {}
        self.title_lock = threading.Lock()
        self.last_title_attempt = 0
        self.title_cursor = 0

    def refresh_titles(self):
        if not self.title_lock.acquire(blocking=False):
            return
        try:
            if time.time() - self.last_title_attempt < 60:
                return
            self.last_title_attempt = time.time()
            records = read_json(self.folder / "registry.json", {})
            if not isinstance(records, dict) or not records:
                return
            # The official title reader accepts at most 100 exact IDs per call.
            # Advance before I/O so unnamed tasks or a failed batch cannot
            # starve the remaining registered tasks, including cached names.
            ids = sorted(records)
            start = self.title_cursor % len(ids)
            batch = ids[start:start + 100]
            self.title_cursor = (start + len(batch)) % len(ids)
            found = fetch_conversation_titles(batch, codex_command())
            self.titles = {**self.titles, **found}
            write_json(self.folder / "titles.json", self.titles)
        except Exception:
            # Metadata availability does not change token accounting. Keep
            # cached names or stable local fallback names until the next try.
            pass
        finally:
            self.title_lock.release()

    def _begin_refresh(self):
        if not self.refresh_lock.acquire(blocking=False):
            return {"status": "running", "retryAfterSeconds": 0}
        now = time.time()
        remaining = QUOTA_REFRESH_COOLDOWN - (now - self.last_attempt)
        if self.last_attempt and now >= self.last_attempt and remaining > 0:
            self.refresh_lock.release()
            return {"status": "throttled", "retryAfterSeconds": math.ceil(remaining)}
        self.last_attempt = now
        return {"status": "started", "retryAfterSeconds": 0}

    def _refresh_quota(self):
        try:
            try:
                self.quota = fetch_quota()
                self.quota_verified = True
                self.credit_ledger = record_balance(self.credit_ledger, self.quota)
                with contextlib.suppress(OSError):
                    write_json(self.folder / "credit-balances.json", self.credit_ledger)
            except Exception as exc:
                self.quota_verified = False
                self.quota = {**self.quota, "error": str(exc) if isinstance(exc, RuntimeError) else "额度查询暂不可用"}
            # Persist a failure marker too, so restarting cannot revive a failed
            # observation as fresh. A cache-write failure does not undo a fetch.
            with contextlib.suppress(OSError):
                write_json(self.folder / "quota.json", self.quota)
        finally:
            self.refresh_lock.release()

    def refresh(self):
        state = self._begin_refresh()
        if state["status"] == "started":
            self._refresh_quota()
        return state

    def start_refresh(self):
        state = self._begin_refresh()
        if state["status"] == "started":
            try:
                threading.Thread(target=self._refresh_quota, daemon=True).start()
            except RuntimeError:
                self.refresh_lock.release()
                raise
        return state

    def settings(self):
        try:
            saved = read_json(self.folder / "settings.json", DEFAULT_SETTINGS)
            if isinstance(saved, dict) and "pricingMode" not in saved:
                # Preserve intentional old custom pricing; a blank legacy rate
                # has no conversion to migrate, so use the new USD default.
                saved = ({**saved, "pricingMode": "custom"} if saved.get("ratePerMillion") not in (None, "")
                         else dict(DEFAULT_SETTINGS))
            if isinstance(saved, dict) and "currencyCode" not in saved:
                # Migrate legacy meter settings to the bundled non-Fast default.
                # This only updates meter settings, not Codex settings.
                saved = {**saved, "speedMode": "standard"}
            return validate_settings(saved)
        except ValueError:
            return deepcopy(DEFAULT_SETTINGS)

    def start_official_usage_refresh(self, thread_id):
        records = read_json(self.folder / "registry.json", {})
        if (not isinstance(thread_id, str) or not 0 < len(thread_id) <= 200
                or not isinstance(records, dict) or thread_id not in records):
            raise ValueError("仅能同步已经登记的对话")
        if not self.official_usage_lock.acquire(blocking=False):
            return {"status": "running", "retryAfterSeconds": 0}
        now = time.time()
        scope = self.quota.get("accountScope")
        if scope is None or not self.quota_verified:
            self.official_usage_lock.release()
            raise ValueError("请先刷新官方余额，确认当前账户后再同步对话记录")
        attempt_key = (scope, thread_id)
        previous = self.official_usage_attempts.get(attempt_key, 0)
        if previous and previous <= now < previous + 60:
            self.official_usage_lock.release()
            return {"status": "throttled", "retryAfterSeconds": math.ceil(previous + 60 - now)}
        self.official_usage_attempts[attempt_key] = now
        self.official_usage_thread = thread_id
        def run():
            try:
                result = fetch_thread_usage(thread_id, codex_command())
                # Never reuse records under a different observed account.
                if self.quota.get("accountScope") != scope:
                    return
                # Scope and value are one immutable observation: a snapshot
                # must never pair an old value with a concurrently updated ID.
                self.official_usage[thread_id] = {**result, "accountScope": scope}
            except Exception:
                if self.quota.get("accountScope") != scope:
                    return
                old = self.official_usage.get(thread_id, {})
                old = old if old.get("accountScope") == scope else {}
                self.official_usage[thread_id] = {**old,
                    "accountScope": scope,
                    "status": "stale" if old.get("credits") is not None else "unavailable",
                    "error": "官方用量同步失败，请稍后重试或查看官方 Usage 页面"}
            finally:
                self.official_usage_thread = None
                self.official_usage_lock.release()
        try:
            threading.Thread(target=run, daemon=True).start()
        except RuntimeError:
            self.official_usage_thread = None
            self.official_usage_lock.release()
            raise
        return {"status": "started", "retryAfterSeconds": 0}

    def snapshot(self):
        # A reader owns an incremental counter baseline; concurrent page/MCP
        # requests must not advance the same file twice.
        with self.snapshot_lock:
            return self._snapshot()

    def _snapshot(self):
        self.last_client = time.time()
        registry_path = self.folder / "registry.json"
        records = read_json(registry_path, None)
        registry_failed = (records is None and registry_path.exists()) or (records is not None and not isinstance(records, dict))
        turns, errors = [], []
        if registry_failed:
            records = {}
            errors.append("本地登记记录格式无效")
        elif records is None:
            records = {}
        settings = self.settings()
        readings, read_errors, successful_reads, metadata = {}, {}, 0, {}
        self.cache = {key: reader for key, reader in self.cache.items() if key in records}
        self.reader_incomplete = {key: value for key, value in self.reader_incomplete.items() if key in records}
        # Freeze this pass's shares before reads change their completion state.
        # Caught-up files are still checked; pending history gets the larger share.
        weights = {key: 100 if self.reader_incomplete.get(key, True) else 1 for key in records}
        total_weight = sum(weights.values())
        for thread_id, record in records.items():
            try:
                path = Path(record["path"])
                reader = self.cache.get(thread_id)
                if reader is None:
                    reader = self.cache[thread_id] = UsageLogReader(thread_id)
                result = reader.read(path, time_budget=weights[thread_id] / total_weight)
                reading = result.get("reading")
                self.reader_incomplete[thread_id] = not (isinstance(reading, dict) and reading.get("complete") is True)
                successful_reads += 1
                readings[thread_id] = result.get("reading")
                metadata[thread_id] = result.get("conversationMetadata", {})
                turns.extend(dict(turn) for turn in result["turns"])
                if result.get("warnings"):
                    errors.extend(result["warnings"])
            except Exception:
                self.reader_incomplete[thread_id] = True
                # Do not display stale last-good token totals as a new successful read.
                # Keep the reader's rejection state: a foreign session found in
                # a later chunk must not revive an earlier prefix next refresh.
                read_errors[thread_id] = "这段对话的记录暂时无法读取，请稍后刷新。"
                errors.append("某个已登记任务的记录暂不可读或格式已变化")
        turns.sort(key=lambda turn: turn.get("startedAt") or 0, reverse=True)
        conversations = conversation_catalog(records, turns, self.titles, metadata)
        for conversation in conversations:
            conversation["reading"] = readings.get(conversation["id"])
            conversation["readError"] = read_errors.get(conversation["id"])
            if conversation["reading"] and not conversation["reading"]["complete"]:
                # A historical prefix may end in an old unfinished turn.
                conversation["activeTurnCount"] = 0
        for turn in turns:
            turn["pricing"] = estimate_turn(turn, settings)
            turn["amount"] = turn["pricing"]["amount"]
            turn["models"] = turn["pricing"].get("models", [])
        periods = summarize_periods(turns, settings,
                                   reading_incomplete=any(r and not r["complete"] for r in readings.values()),
                                   read_error_count=len(read_errors) + int(registry_failed))
        # Retain one recent record for older tasks so the global history cap
        # cannot make an explicitly selected registered task appear empty.
        visible_turns = turns[:200]
        represented = {turn["threadId"] for turn in visible_turns}
        for turn in turns[200:]:
            if turn["threadId"] not in represented:
                visible_turns.append(turn)
                represented.add(turn["threadId"])
        status = "partial" if errors else "active" if records else "waiting"
        message = ("；".join(dict.fromkeys(errors)) if errors else
                   "已登记任务的本地记录；每条仅统计该任务自身，子代理单独列出" if records else
                   "等待登记任务。安装后在 Codex 中审阅并信任本插件 Hooks，新问题才会自动登记。")
        quota_snapshot = deepcopy(self.quota)
        official_records = list(self.official_usage.items())
        return {"monitoring": {"status": status, "message": message, "lastUpdate": time.time() if successful_reads else None,
                               "coverage": {"registeredTasks": len(records), "readableTasks": successful_reads,
                                            "unreadableTasks": len(read_errors),
                                            "backfillingTasks": sum(bool(r and not r["complete"]) for r in readings.values())}},
                "quota": quota_view(quota_snapshot, now=time.time(), last_attempt=self.last_attempt, refreshing=self.refresh_lock.locked(), validated=self.quota_verified),
                "officialCredits": credit_view(quota_snapshot, self.credit_ledger, settings, now=time.time(), validated=self.quota_verified),
                "officialUsage": {thread_id: {**thread_usage_view(record, now=time.time(),
                    validated=self.quota_verified and quota_snapshot.get("accountScope") is not None),
                    "refreshing": self.official_usage_thread == thread_id}
                    for thread_id, record in official_records
                    if thread_id in records and quota_snapshot.get("accountScope") is not None
                    and record.get("accountScope") == quota_snapshot.get("accountScope")},
                "turns": visible_turns, "conversations": conversations, "periods": periods, "settings": settings, "csrfToken": self.csrf,
                "fxReference": {**FX_REFERENCE, "customized": any(decimal.Decimal(settings["exchangeRates"][key]) != decimal.Decimal(EXCHANGE_RATES[key]) for key in EXCHANGE_RATES)}}


class Handler(BaseHTTPRequestHandler):
    server_version = "CodexUsageMeter/0.10.1"

    def log_message(self, *_):
        pass

    def allowed(self, write=False):
        origin = "http://127.0.0.1:" + str(self.server.server_port)
        if self.headers.get("Host") != origin[7:]:
            return False
        if self.headers.get("Origin") not in (None, origin):
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            return False
        if write:
            supplied = self.headers.get("X-Meter-Token", "")
            if not supplied.isascii() or not secrets.compare_digest(supplied, self.server.meter.csrf):
                return False
        return True

    def respond(self, value, status=200, kind="application/json; charset=utf-8"):
        data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self.allowed():
            return self.respond({"error": "拒绝跨来源访问"}, 403)
        if self.path == "/":
            return self.respond((ROOT / "web/index.html").read_bytes(), kind="text/html; charset=utf-8")
        if self.path == "/api/state":
            snapshot = self.server.meter.snapshot()
            if any(row["titleSource"] == "fallback" for row in snapshot["conversations"]) and time.time() - self.server.meter.last_title_attempt >= 60:
                threading.Thread(target=self.server.meter.refresh_titles, daemon=True).start()
            return self.respond(snapshot)
        if self.path == "/health":
            return self.respond({"app": "codex-usage-meter", "version": "0.10.1", "pid": os.getpid()})
        self.respond({"error": "不存在"}, 404)

    def do_POST(self):
        if not self.allowed(write=True):
            return self.respond({"error": "请求校验失败，请刷新面板"}, 403)
        if self.path == "/api/shutdown":
            self.respond({"ok": True})
            self.server.stop_event.set()
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if self.path == "/api/refresh":
            return self.respond({"ok": True, "refresh": self.server.meter.start_refresh()})
        if self.path not in ("/api/settings", "/api/conversation-label", "/api/official-usage-refresh"):
            return self.respond({"error": "不存在"}, 404)
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 1 or size > 4096 or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ValueError("请求格式无效")
            value = json.loads(self.rfile.read(size))
            if self.path == "/api/official-usage-refresh":
                if not isinstance(value, dict) or set(value) != {"threadId"}:
                    raise ValueError("同步请求字段无效")
                return self.respond({"ok": True, "refresh": self.server.meter.start_official_usage_refresh(value["threadId"])})
            if self.path == "/api/conversation-label":
                return self.respond(set_conversation_label(self.server.meter.folder, value))
            settings = validate_settings(value)
            write_json(self.server.meter.folder / "settings.json", settings)
            self.respond({"ok": True, "settings": settings})
        except (ValueError, UnicodeError) as exc:
            self.respond({"error": str(exc)}, 400)


class MeterHTTPServer(ThreadingHTTPServer):
    def __init__(self, *args, **kwargs):
        self.activity_lock = threading.Lock()
        self.active_requests = 0
        self.last_request_at = time.monotonic()
        self.idle_timeout = 0
        self.idle_shutdown_started = False
        super().__init__(*args, **kwargs)

    def process_request(self, request_socket, client_address):
        with self.activity_lock:
            self.active_requests += 1
            self.last_request_at = time.monotonic()
        try:
            super().process_request(request_socket, client_address)
        except BaseException:
            with self.activity_lock:
                self.active_requests -= 1
            raise

    def process_request_thread(self, request_socket, client_address):
        try:
            request_socket.settimeout(15)
            super().process_request_thread(request_socket, client_address)
        finally:
            with self.activity_lock:
                self.active_requests -= 1
                self.last_request_at = time.monotonic()

    def service_actions(self):
        with self.activity_lock:
            idle = (self.idle_timeout > 0 and not self.active_requests and
                    not self.idle_shutdown_started and
                    time.monotonic() - self.last_request_at >= self.idle_timeout)
            if idle:
                self.idle_shutdown_started = True
        if idle:
            self.stop_event.set()
            # shutdown() must run outside the serve_forever() thread.
            threading.Thread(target=self.shutdown, daemon=True).start()

    def server_bind(self):
        if is_windows():
            # Windows SO_REUSEADDR can let a second process share the same port.
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # HTTPServer.server_bind performs an unbounded reverse-DNS lookup.
        # The panel only uses a numeric loopback address, so DNS is unnecessary.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def serve(folder, port=0, *, launchd_socket=None, idle_timeout=0):
    if (type(port) is not int or not 0 <= port <= 65535 or
            type(idle_timeout) is not int or not 0 <= idle_timeout <= 86400 or
            launchd_socket not in (None, SOCKET_NAME)):
        raise ValueError("本地服务启动参数无效")
    with locked(folder, "service"):
        endpoint_path = folder / "endpoint.json"
        endpoint = read_json(endpoint_path, {})
        saved_url = endpoint.get("url", "") if isinstance(endpoint, dict) else ""
        if port == 0:
            if local_url(saved_url):
                port = parse.urlsplit(saved_url).port
        if launchd_socket and not 1 <= port <= 65535:
            raise ValueError("按需服务需要有效的固定端口")
        if launchd_socket and endpoint_path.exists() and saved_url != "http://127.0.0.1:" + str(port) + "/":
            raise RuntimeError("按需监听与已保存的面板地址不一致，未更换地址")
        meter = Meter(folder)
        try:
            if launchd_socket:
                listener = activate_launchd_socket(launchd_socket, port)
                server = adopt_http_socket(MeterHTTPServer, Handler, listener, port)
            else:
                server = MeterHTTPServer(("127.0.0.1", port), Handler)
        except OSError as exc:
            if port:
                raise RuntimeError("本地面板无法使用端口 " + str(port) + "，未更换地址") from exc
            raise
        server.meter = meter
        # Finish accepted requests before the worker exits. launchd retains the
        # listener so a later browser visit can activate a fresh worker.
        server.daemon_threads = False
        server.idle_timeout = idle_timeout
        server.stop_event = threading.Event()
        endpoint = {"url": "http://127.0.0.1:" + str(server.server_port) + "/", "pid": os.getpid()}
        write_json(folder / "endpoint.json", endpoint)
        def refresh_loop():
            while not server.stop_event.is_set():
                if time.time() - meter.last_client < 120:
                    meter.refresh()
                    if time.time() - meter.last_title_attempt >= 300:
                        meter.refresh_titles()
                server.stop_event.wait(60)
        threading.Thread(target=refresh_loop, daemon=True).start()
        print(json.dumps(endpoint), flush=True)
        try:
            server.serve_forever()
        finally:
            server.stop_event.set()
            server.server_close()


def local_url(url):
    try:
        parsed = parse.urlsplit(url)
        return (parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and
                parsed.username is None and parsed.password is None and
                parsed.port is not None and 0 < parsed.port <= 65535 and
                not parsed.query and not parsed.fragment and parsed.path in ("/", "/health", "/api/state"))
    except (ValueError, TypeError):
        return False


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise RuntimeError("本地面板不允许重定向")


def local_get(url):
    if not local_url(url):
        raise RuntimeError("本地面板地址无效")
    opener = request.build_opener(request.ProxyHandler({}), NoRedirect())
    with opener.open(url, timeout=2) as response:
        return json.load(response)


def local_shutdown(url, token):
    if (not local_url(url) or parse.urlsplit(url).path != "/" or
            not isinstance(token, str) or not token or not token.isascii() or len(token) > 256):
        raise RuntimeError("本地面板停止请求无效")
    opener = request.build_opener(request.ProxyHandler({}), NoRedirect())
    message = request.Request(url + "api/shutdown", data=b"{}", method="POST", headers={
        "Content-Type": "application/json", "Origin": url.rstrip("/"), "X-Meter-Token": token})
    with opener.open(message, timeout=2) as response:
        result = json.load(response)
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("本地面板未确认停止请求")


def stop_service(folder):
    """Ask only the authenticated meter at its saved local address to shut down."""
    endpoint = read_json(folder / "endpoint.json", {})
    url = endpoint.get("url", "") if isinstance(endpoint, dict) else ""
    if not local_url(url) or parse.urlsplit(url).path != "/":
        return False
    try:
        health = local_get(url + "health")
    except OSError:
        return False
    # A demand connection can wake a new worker and replace the saved PID.
    endpoint = read_json(folder / "endpoint.json", {})
    if (not isinstance(health, dict) or health.get("app") != "codex-usage-meter" or
            not isinstance(endpoint, dict) or endpoint.get("url") != url or
            health.get("pid") != endpoint.get("pid")):
        return False
    state = local_get(url + "api/state")
    if not isinstance(state, dict):
        raise RuntimeError("本地面板状态无效，未停止任何进程")
    local_shutdown(url, state.get("csrfToken"))
    return True


def ensure_service(folder):
    with locked(folder, "launch"):
        mode = read_service_mode(folder)
        endpoint = read_json(folder / "endpoint.json", {})
        url = endpoint.get("url", "") if isinstance(endpoint, dict) else ""
        if mode:
            expected_url = "http://127.0.0.1:" + str(mode["port"]) + "/"
            if url and url != expected_url:
                raise RuntimeError("按需服务与保存的面板地址不一致，未更换地址")
            url = expected_url
        if local_url(url):
            try:
                if local_get(url + "health").get("app") == "codex-usage-meter":
                    return url
            except Exception:
                pass
        if not mode:
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--data-dir", str(folder), "serve"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             **subprocess_options(background=True))
        deadline = time.monotonic() + (12 if mode else 5)
        while time.monotonic() < deadline:
            endpoint = read_json(folder / "endpoint.json", {})
            url = expected_url if mode else endpoint.get("url", "") if isinstance(endpoint, dict) else ""
            try:
                if local_url(url) and local_get(url + "health").get("app") == "codex-usage-meter":
                    return url
            except Exception:
                pass
            time.sleep(0.1)
        raise RuntimeError("本地按需入口未能唤醒服务，请检查服务安装状态" if mode else "本地面板未能启动")


def handle_hook(folder, data):
    if not isinstance(data, dict) or data.get("hook_event_name") not in EVENTS:
        return
    # Prompt bodies, tool arguments and agent messages are deliberately ignored.
    path, thread_id = data.get("transcript_path"), data.get("session_id")
    if isinstance(path, str) and isinstance(thread_id, str):
        register(folder, path, thread_id)
    if data.get("hook_event_name") == "SubagentStop":
        child_path, child_id = data.get("agent_transcript_path"), data.get("agent_id")
        if isinstance(child_path, str) and isinstance(child_id, str):
            register(folder, child_path, child_id)


def mcp(folder):
    tools = [
        {"name": "open_usage_meter", "description": "打开本地 Codex 用量面板，返回可在应用内浏览器打开的地址。", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}, "annotations": {"readOnlyHint": True, "openWorldHint": False}},
        {"name": "get_usage_summary", "description": "读取已登记任务的 Token、credits 及金额估算和官方额度快照；金额不是实际扣款，Token 不代表订阅剩余余额。", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}, "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    ]
    for line in sys.stdin:
        req = None
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                raise ValueError("Invalid Request")
            if "id" not in req:
                continue
            method = req.get("method")
            if method == "initialize":
                result = {"protocolVersion": req.get("params", {}).get("protocolVersion", "2024-11-05"), "capabilities": {"tools": {}}, "serverInfo": {"name": "codex-usage-meter", "version": "0.10.1"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": tools}
            elif method == "tools/call":
                name = req.get("params", {}).get("name")
                if name not in {tool["name"] for tool in tools}:
                    raise ValueError("未知工具")
                url = ensure_service(folder)
                value = {"url": url, "message": "在 Codex 应用内浏览器打开此地址"}
                if name == "get_usage_summary":
                    value = local_get(url + "api/state")
                    value.pop("csrfToken", None)
                    value["turns"] = value["turns"][:10]
                result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}
            else:
                print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32601, "message": "Method not found"}}), flush=True)
                continue
            print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}, ensure_ascii=False), flush=True)
        except Exception:
            if isinstance(req, dict) and "id" in req:
                print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32603, "message": "用量工具暂不可用；请检查本地运行环境"}}), flush=True)
            elif req is None:
                print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}), flush=True)


def main():
    # MCP and hook JSON are UTF-8 even when Windows redirects a legacy codepage.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir")
    sub = parser.add_subparsers(dest="command", required=True)
    service = sub.add_parser("serve")
    service.add_argument("--port", type=int, default=0)
    service.add_argument("--launchd-socket", choices=[SOCKET_NAME])
    service.add_argument("--idle-timeout", type=int, default=0)
    sub.add_parser("open")
    sub.add_parser("mcp")
    sub.add_parser("hook")
    reg = sub.add_parser("register")
    reg.add_argument("--transcript", required=True)
    reg.add_argument("--thread-id", required=True)
    sub.add_parser("summary")
    sub.add_parser("stop")
    args = parser.parse_args()
    folder = data_dir(args.data_dir)
    if args.command == "serve":
        serve(folder, args.port, launchd_socket=args.launchd_socket, idle_timeout=args.idle_timeout)
    elif args.command == "hook":
        # A meter must never block, continue, or alter the agent's work.
        try:
            payload = sys.stdin.read(2_000_000)
            handle_hook(folder, json.loads(payload))
        except Exception:
            pass
    elif args.command == "register":
        print(json.dumps(register(folder, args.transcript, args.thread_id)))
    elif args.command == "mcp":
        mcp(folder)
    elif args.command == "open":
        print(ensure_service(folder))
    elif args.command == "summary":
        state = local_get(ensure_service(folder) + "api/state")
        state.pop("csrfToken", None)
        print(json.dumps(state, ensure_ascii=False, indent=2))
    elif args.command == "stop":
        print("已请求停止用量面板" if stop_service(folder) else "没有可确认的本地用量服务")


if __name__ == "__main__":
    main()
