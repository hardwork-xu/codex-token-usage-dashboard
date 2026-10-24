"""Separate acknowledged historical source gaps from current collection health.

This presentation adapter never changes token observations, prices, original
quality/status fields, or the archive. Unknown costs remain unknown.
"""
from __future__ import annotations


def _count(value):
    return value if type(value) is int and value >= 0 else 0


def _acknowledged(view):
    return {row["threadId"]: row for row in view.get("tasks", [])
            if isinstance(row, dict) and isinstance(row.get("threadId"), str)}


def decorate_collection(collection, view, sources):
    """Use this collector's own source states, not a newer snapshot's counts."""
    ack = _acknowledged(view)
    folded = {tid for tid in ack if sources.get(tid, {}).get("sourceError") == "source_missing"}
    historical = sum(bool(sources.get(tid, {}).get("unreadable")) for tid in folded)
    active_unreadable = max(0, _count(collection.get("unreadableTasks")) - historical)
    changed = sum(bool(row.get("retained")) and not row.get("pending")
                  for tid, row in sources.items() if tid not in folded)
    continuity = sum(bool(row.get("continuationGap")) for tid, row in sources.items() if tid not in folded)
    error = (_count(collection.get("writeErrors")) > 0 or view.get("status") == "error"
             or collection.get("status") == "error")
    status = ("error" if error else "partial" if active_unreadable or changed or continuity
              else "collecting" if _count(collection.get("pendingTasks")) else "ready")
    code = ("historical_gap_metadata_error" if view.get("status") == "error" else
            collection.get("errorCode") if error or active_unreadable else
            "source_changed" if changed else "continuation_gap" if continuity else None)
    collection.update(historicalGapTasks=historical, activeUnreadableTasks=active_unreadable,
                      activeStatus=status, activeErrorCode=code)
    return collection


def decorate_snapshot(snapshot, view, sources):
    monitoring = snapshot["monitoring"]
    monitoring["historicalGaps"] = view
    ack = _acknowledged(view)
    conversations = snapshot.get("conversations", [])
    # A later missing-file check cannot erase a different error observed in
    # this scan (for example permission or identity failure before removal).
    folded = {row.get("id") for row in conversations if row.get("id") in ack
              and row.get("readErrorCode") == "source_missing"}
    for conversation in conversations:
        entry = ack.get(conversation.get("id"))
        if entry is None or conversation.get("id") not in folded:
            continue
        archive = conversation.get("archive")
        if not isinstance(archive, dict):
            archive = {"source": "none", "reason": "source_unavailable", "savedAt": None}
            conversation["archive"] = archive
        archive.update(historicalGap=True, hasSavedUsage=bool(entry.get("hasSavedUsage")),
                       acknowledgedAt=entry.get("acknowledgedAt"))
    active_unreadable = sum(bool(row.get("readError")) for row in conversations if row.get("id") not in folded)
    active_backfilling = sum((row.get("reading") or {}).get("complete") is False
                            for row in conversations if row.get("id") not in folded)
    active_retained = sum((row.get("archive") or {}).get("source") == "archive"
                          for row in conversations if row.get("id") not in folded)
    continuity = sum(((row.get("archive") or {}).get("continuation") or {}).get("status")
                     == "continuation_with_gap" for row in conversations if row.get("id") not in folded)
    coverage = monitoring.get("coverage", {})
    coverage.update(activeUnreadableTasks=active_unreadable, activeBackfillingTasks=active_backfilling)
    archive = monitoring.get("archive", {})
    archive.update(activeRetainedTasks=active_retained,
                   activeStatus="error" if _count(archive.get("writeErrors")) else
                   "partial" if active_retained or continuity else "ready")
    collection = monitoring.get("collection", {})
    decorate_collection(collection, view, sources)
    error = view.get("status") == "error" or archive.get("activeStatus") == "error" or collection.get("activeStatus") == "error"
    monitoring["activeStatus"] = ("error" if error else "partial" if active_unreadable or continuity
                                  or (active_retained and not active_backfilling) else
                                  "collecting" if active_backfilling else
                                  "active" if conversations else "waiting")
    return snapshot
