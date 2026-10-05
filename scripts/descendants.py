"""Bounded descendant metadata discovery for an explicitly registered root.

Only public thread/list with ancestorThreadId is used. Returned paths are not
registered here: callers must still use the normal usage reader. No prompt,
preview, account identifier, or full server response leaves this module.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import subprocess

from conversations import source_metadata
from rpc_transport import JsonRpcProcess, command_prefix


MAX_PAGES = 5
MAX_PAGE_SIZE = 100
MAX_HEADER_BYTES = 4 * 1024 * 1024
MAX_ANCESTRY = 128
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SOURCE_KINDS = ["subAgent", "subAgentReview", "subAgentCompact", "subAgentThreadSpawn", "subAgentOther"]


def _identifier(value):
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


def _ordinary(info):
    return (stat.S_ISREG(info.st_mode) and not getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _header(path, expected_id):
    """Read only the first identity record of an exact, non-symlink path."""
    if not isinstance(path, (str, os.PathLike)):
        return None
    try:
        target = Path(path)
        if not target.is_absolute() or target.suffix.lower() != ".jsonl":
            return None
        before = target.lstat()
        if not _ordinary(before):
            return None
        flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) |
                 getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
        fd = os.open(target, flags)
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not _ordinary(opened) or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                return None
            line = stream.readline(MAX_HEADER_BYTES + 1)
            after = target.lstat()
            if (not _ordinary(after) or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
                    or len(line) > MAX_HEADER_BYTES or not line.endswith(b"\n")):
                return None
        value = json.loads(line)
        payload = value.get("payload") if isinstance(value, dict) else None
        if (not isinstance(value, dict) or value.get("type") != "session_meta" or not isinstance(payload, dict)
                or payload.get("id") != expected_id):
            return None
        info = source_metadata(payload)
        parent = info.get("parentThreadId")
        if info["sourceType"] == "subagent":
            if _identifier(parent) is None or parent == expected_id:
                return None
            if payload.get("parent_thread_id") not in (None, parent):
                return None
        return {"threadId": expected_id, "path": str(target), "parentThreadId": parent,
                "sourceType": info["sourceType"]}
    except (OSError, ValueError, TypeError, RecursionError):
        return None


def discover_descendants(records, root_id, codex_binary, *, cursor=None, archived=False,
                         max_pages=2, page_size=100, timeout=15.0):
    """Discover one bounded batch, using registered ancestors across page calls.

    Results are sorted oldest-first so ancestors are normally registered before
    their later descendants. A caller can resume nextCursor after registering
    this batch, passing a fresh registry snapshot. A missing ancestor is deferred
    rather than guessed. Query archived trees separately with archived=True.
    Account switching cannot expand this scope: every admitted path must have a
    fully verified parent chain back to this exact registered root.
    """
    result = {"threads": [], "nextCursor": cursor, "complete": False, "pages": 0, "checked": 0,
              "rejected": 0, "deferred": 0, "error": None}
    if (not isinstance(records, dict) or _identifier(root_id) is None
            or not isinstance(records.get(root_id), dict)):
        raise ValueError("仅可同步已登记主任务的子任务。")
    if (type(max_pages) is not int or not 1 <= max_pages <= MAX_PAGES
            or type(page_size) is not int or not 1 <= page_size <= MAX_PAGE_SIZE
            or isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not 0 < timeout <= 30):
        raise ValueError("子任务同步预算无效。")
    if (type(archived) is not bool or cursor is not None and
            (not isinstance(cursor, str) or not cursor or len(cursor) > 4096)):
        raise ValueError("子任务同步分页参数无效。")
    root = _header(records[root_id].get("path"), root_id)
    if root is None or root["sourceType"] != "main":
        result["error"] = "主任务身份暂时无法核实，未同步子任务。"
        return result
    prefix = command_prefix(codex_binary)
    nodes, conflicted = {}, set()
    seen_cursors = {cursor} if cursor is not None else set()
    try:
        with JsonRpcProcess([*prefix, "app-server", "--stdio"], timeout=timeout) as rpc:
            rpc.send({"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "codex_usage_meter_descendants", "version": "0.12.0"},
                "capabilities": {"experimentalApi": True}}})
            responses = rpc.responses()

            def receive(request_id):
                for response in responses:
                    if type(response.get("id")) is int and response["id"] == request_id:
                        if "error" in response or not isinstance(response.get("result"), dict):
                            raise ValueError("metadata request failed")
                        return response["result"]
                raise ValueError("metadata response missing")

            receive(1)
            rpc.send({"method": "initialized"})
            for page in range(max_pages):
                request_id = page + 2
                rpc.send({"id": request_id, "method": "thread/list", "params": {
                    "ancestorThreadId": root_id, "sourceKinds": list(_SOURCE_KINDS),
                    "useStateDbOnly": True, "archived": archived, "cursor": result["nextCursor"],
                    "limit": page_size, "sortKey": "created_at", "sortDirection": "asc"}})
                payload = receive(request_id)
                rows, following = payload.get("data"), payload.get("nextCursor")
                if (not isinstance(rows, list) or len(rows) > page_size
                        or following is not None and
                        (not isinstance(following, str) or not following or len(following) > 4096
                         or following in seen_cursors)):
                    raise ValueError("metadata page invalid")
                result["pages"] += 1
                for row in rows:
                    if not isinstance(row, dict):
                        result["rejected"] += 1
                        continue
                    ident, parent, path = row.get("id"), row.get("parentThreadId"), row.get("path")
                    if (_identifier(ident) is None or _identifier(parent) is None
                            or ident in (parent, root_id) or not isinstance(path, str)):
                        result["rejected"] += 1
                        continue
                    candidate = {"threadId": ident, "parentThreadId": parent, "path": path}
                    if ident in nodes and candidate != nodes[ident]:
                        conflicted.add(ident)
                    nodes[ident] = candidate
                result["nextCursor"] = following
                if following is None:
                    result["complete"] = True
                    break
                seen_cursors.add(following)
    except (OSError, ValueError, TimeoutError, subprocess.SubprocessError):
        # Keep verified earlier pages and retry the failed page on the next call.
        result["error"] = "子任务元数据同步未完成，将在后续刷新重试。"

    verified = {root_id: root}
    failed = set(conflicted)

    def header_for(ident):
        if ident in verified:
            return verified[ident]
        if ident in failed:
            return None
        node = nodes.get(ident)
        record = records.get(ident)
        path = node["path"] if node is not None else record.get("path") if isinstance(record, dict) else None
        value = _header(path, ident)
        if (value is None or value["sourceType"] != "subagent" or
                node is not None and value["parentThreadId"] != node["parentThreadId"]):
            failed.add(ident)
            return None
        verified[ident] = value
        return value

    for ident, node in nodes.items():
        result["checked"] += 1
        chain, current = set(), ident
        reached = False
        for _ in range(MAX_ANCESTRY):
            if current == root_id:
                reached = True
                break
            if current in chain or current in conflicted:
                break
            chain.add(current)
            value = header_for(current)
            if value is None:
                break
            current = value["parentThreadId"]
        if reached:
            value = verified[ident]
            result["threads"].append({key: value[key] for key in ("threadId", "path", "parentThreadId")})
        elif current not in nodes and current not in records:
            result["deferred"] += 1
        else:
            result["rejected"] += 1
    return result


__all__ = ["discover_descendants"]
