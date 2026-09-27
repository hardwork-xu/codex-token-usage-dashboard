"""Read names for explicitly registered threads through public Codex metadata RPC.

Documentation: https://learn.chatgpt.com/docs/app-server#read-a-stored-thread-without-resuming
`thread/read` with `includeTurns: false` does not resume a thread or load it into
memory. Only matching thread IDs and sanitized `thread.name` values leave this
module. Preview text, messages, turns, and other metadata are discarded.
"""

from __future__ import annotations

import os
import re
import subprocess
import unicodedata
from typing import Any, Iterable

from rpc_transport import JsonRpcProcess, command_prefix


MAX_THREADS = 100
MAX_TITLE_LENGTH = 120
RPC_TIMEOUT_SECONDS = 15.0
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


def normalize_title(value: Any) -> str | None:
    """Return a short plain title, or None; never infer one from message text."""
    if not isinstance(value, str):
        return None
    # Bound normalization work even if a malformed peer returns a huge title.
    characters = (
        " " if unicodedata.category(char) in {"Cc", "Cf", "Cs"} else char
        for char in value[:4096]
    )
    title = " ".join("".join(characters).split())[:MAX_TITLE_LENGTH].rstrip()
    return title or None


def source_metadata(payload: Any) -> dict:
    """Allowlist identity metadata from an already verified session header.

    Agent paths identify a task in the agent tree, not a filesystem location.
    Never retain the source object, instructions, working directory or preview.
    """
    result = {"sourceType": "unknown", "parentThreadId": None, "agentLabel": None}
    if not isinstance(payload, dict):
        return result
    source = payload.get("source")
    if isinstance(source, str):
        if source in {"cli", "vscode", "exec", "appServer"}:
            result["sourceType"] = "main"
        elif source == "subagent":
            result["sourceType"] = "subagent"
        return result
    if not isinstance(source, dict) or "subagent" not in source:
        return result
    result["sourceType"] = "subagent"
    subagent = source["subagent"]
    spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
    if not isinstance(spawn, dict):
        return result
    parent = spawn.get("parent_thread_id")
    if isinstance(parent, str) and _ID.fullmatch(parent):
        result["parentThreadId"] = parent
    path = spawn.get("agent_path")
    if isinstance(path, str) and len(path) <= 512 and re.fullmatch(r"/root(?:/[A-Za-z0-9_-]{1,80})+", path):
        result["agentLabel"] = path.rsplit("/", 1)[-1]
    else:
        result["agentLabel"] = normalize_title(spawn.get("agent_nickname"))
    return result


def _identifiers(values: Iterable[str], limit: int | None = None) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise ValueError("需要任务编号列表。")
    try:
        iterator = iter(values)
    except TypeError:
        raise ValueError("需要任务编号列表。") from None
    unique: dict[str, None] = {}
    for index, value in enumerate(iterator):
        if limit is not None and index >= limit:
            raise ValueError("每次最多读取 100 个已登记任务的名称。")
        if not isinstance(value, str) or _ID.fullmatch(value) is None:
            raise ValueError("任务编号格式无效。")
        unique[value] = None
    return list(unique)


def display_ids(ids: Iterable[str]) -> dict[str, str]:
    """Use at least six suffix characters, expanding collisions independently.

    Short IDs are shown in full. Duplicate input IDs share one label. A full ID
    that is itself another ID's suffix remains distinct as the longer ID grows.
    """
    values = _identifiers(ids)
    lengths = {value: min(6, len(value)) for value in values}
    while True:
        groups: dict[str, list[str]] = {}
        for value in values:
            groups.setdefault(value[-lengths[value]:], []).append(value)
        collisions = [group for group in groups.values() if len(group) > 1]
        if not collisions:
            return {value: value[-lengths[value]:] for value in values}
        for group in collisions:
            for value in group:
                if lengths[value] < len(value):
                    lengths[value] += 1


def fetch_conversation_titles(thread_ids: Iterable[str], codex_binary: str | os.PathLike[str] | list[str]) -> dict[str, str]:
    """Read official names for at most 100 exact, caller-supplied thread IDs.

    `codex_binary` is an already resolved executable path or command prefix; this function does no
    file discovery. It starts one documented stdio server, uses only initialize,
    initialized, and thread/read(includeTurns=False), and makes no model calls.
    Missing names and per-ID failures are omitted. Connection/protocol failures
    return names already read, if any. The RPC deadline is 15 seconds, followed
    by bounded process and I/O-thread cleanup. Invalid input raises ValueError.
    """
    ids = _identifiers(thread_ids, MAX_THREADS)
    if not ids:
        return {}
    prefix = command_prefix(codex_binary)
    names: dict[str, str] = {}
    try:
        with JsonRpcProcess([*prefix, "app-server", "--stdio"], timeout=RPC_TIMEOUT_SECONDS) as rpc:
            rpc.send({"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "codex_usage_meter_titles", "version": "0.8.0"},
            }})
            initialized = False
            position = 0
            request_id = 2
            for response in rpc.responses():
                response_id = response.get("id")
                # Reject bool IDs; in Python True otherwise equals request ID 1.
                if type(response_id) is not int:
                    continue
                if not initialized:
                    if response_id != 1:
                        continue
                    if "error" in response or not isinstance(response.get("result"), dict):
                        return names
                    initialized = True
                    rpc.send({"method": "initialized"})
                else:
                    if response_id != request_id:
                        continue
                    expected_id = ids[position]
                    result = response.get("result")
                    if "error" not in response and isinstance(result, dict):
                        thread = result.get("thread")
                        if isinstance(thread, dict) and thread.get("id") == expected_id:
                            title = normalize_title(thread.get("name"))
                            if title is not None:
                                names[expected_id] = title
                    position += 1
                    request_id += 1
                    if position >= len(ids):
                        return names
                rpc.send({"id": request_id, "method": "thread/read", "params": {
                    "threadId": ids[position], "includeTurns": False,
                }})
    except (OSError, ValueError, TimeoutError, subprocess.SubprocessError):
        # Failure details may contain local paths or server data; return neither.
        return names
    return names


__all__ = ["fetch_conversation_titles", "normalize_title", "display_ids", "source_metadata"]
