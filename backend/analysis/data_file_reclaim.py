"""
data_file_reclaim.py
--------------------
Standalone "Data File Space Reclamation" feature — separate from index
maintenance by design.

Reclaims excess free space from ROWS data files while protecting integrity and
performance:

  Phase 1 (safe, default): DBCC SHRINKFILE(name, TRUNCATEONLY) — releases only
    trailing free space. Moves ZERO data pages, so it causes ZERO fragmentation.
  Phase 2 (gate): re-measure against the dynamic 16% buffer target
    (Target = Used / DATA_FILE_BUFFER_RATIO). If TRUNCATEONLY didn't reach it,
    "Deep Compaction" is OFFERED but never auto-run — the UI must explicitly
    confirm it (the free space is interleaved and needs page moves).
  Phase 3 (deep, opt-in): DBCC SHRINKFILE(name, target) to move pages down, then
    IMMEDIATELY rebuild the now-fragmented indexes (correct sequencing) by
    reusing index_fragmentation.execute().
  Phase 4 (lock-aware): the shrink runs under SET LOCK_TIMEOUT so it BACKS OFF on
    a blocking lock — it reports the blocking SPID and aborts gracefully. It
    NEVER kills another session.

Pure target/decision math lives in target_mb()/shrink_required() (unit-tested
without a DB). Nothing here kills sessions or runs page-level DBCC commands.
"""

from __future__ import annotations
import logging
import math
import threading
from typing import Any, Callable, Optional
import pyodbc
from config import (
    DATA_FILE_BUFFER_RATIO,
    DATA_FILE_RECLAIM_MIN_MB,
    SHRINK_LOCK_TIMEOUT_MS,
)
import analysis.index_fragmentation as ifrag

logger = logging.getLogger(__name__)

ISSUE_ID   = "data_file_reclaim"
ISSUE_NAME = "Data File Space Reclamation"

_PAGE_MB = 8 / 1024  # one 8 KB page in MB


# ─────────────────────────────────────────────────────────────────────────────
# Pure helpers (unit-tested without a DB)
# ─────────────────────────────────────────────────────────────────────────────

def target_mb(used_mb: float, ratio: float = DATA_FILE_BUFFER_RATIO) -> int:
    """Ideal file size leaving a (1-ratio) free cushion, rounded UP to an int MB."""
    if used_mb <= 0:
        return 0
    return int(math.ceil(used_mb / ratio))


def shrink_required(size_mb: float, target: int, min_reclaim_mb: int = DATA_FILE_RECLAIM_MIN_MB) -> bool:
    """
    True when the current physical file is larger than the target by at least the
    minimum worthwhile amount (free pool exceeds the 16% buffer). False means the
    file is already tightly packed — nothing to do.
    """
    return (size_mb - target) >= min_reclaim_mb


# ─────────────────────────────────────────────────────────────────────────────
# Shared metadata query
# ─────────────────────────────────────────────────────────────────────────────

def _data_files(cursor) -> list[dict]:
    """Per-ROWS-data-file size/used/target snapshot for the current database."""
    cursor.execute("""
        SELECT df.name AS logical_name,
               CAST(df.size AS BIGINT)                       AS size_pages,
               CAST(FILEPROPERTY(df.name, 'SpaceUsed') AS BIGINT) AS used_pages
        FROM sys.database_files df
        WHERE df.type = 0          -- 0 = ROWS (data); excludes LOG
          AND df.state_desc = 'ONLINE'
        ORDER BY df.size DESC
    """)
    files = []
    for name, size_pages, used_pages in cursor.fetchall():
        size = round(float(size_pages) * _PAGE_MB, 2)
        used = round(float(used_pages) * _PAGE_MB, 2) if used_pages is not None else 0.0
        tgt = target_mb(used)
        files.append({
            "logical_name":   name,
            "size_mb":        size,
            "used_mb":        used,
            "free_mb":        round(size - used, 2),
            "target_mb":      tgt,
            "reclaimable_mb": max(0, round(size - tgt, 2)),
            "shrink_required": shrink_required(size, tgt),
        })
    return files


# ─────────────────────────────────────────────────────────────────────────────
# Analyze
# ─────────────────────────────────────────────────────────────────────────────

def analyze(conn: pyodbc.Connection) -> dict[str, Any]:
    cursor = conn.cursor()
    files = _data_files(cursor)
    actionable = [f for f in files if f["shrink_required"]]
    total_reclaimable = round(sum(f["reclaimable_mb"] for f in actionable), 2)

    base = {
        "issue_id":   ISSUE_ID,
        "issue_name": ISSUE_NAME,
        "analysis_note": (
            f"Target leaves a {round((1 - DATA_FILE_BUFFER_RATIO) * 100)}% free cushion "
            "(Used / 0.84). Phase 1 (TRUNCATEONLY) is non-fragmenting; Deep Compaction "
            "moves pages and is offered only on explicit confirmation, then rebuilds the "
            "affected indexes."
        ),
    }

    if not actionable:
        return {
            **base,
            "severity": "Low",
            "affected_objects": files,
            "current_metrics": {"data_files": len(files), "reclaimable_mb": 0,
                                "actionable_files": 0},
            "recommended_action": ("Data file layout is already optimized — free space is "
                                   "within the 16% safety buffer. No reclamation needed."),
            "estimated_impact": "N/A",
            "executable": False, "eligible_for_fix": False,
            "blocking_reason": "No data file holds excess free space beyond the buffer.",
        }

    severity = "High" if total_reclaimable > 5_000 else "Medium" if total_reclaimable > 500 else "Low"
    return {
        **base,
        "severity": severity,
        "affected_objects": files,
        "current_metrics": {
            "data_files":       len(files),
            "actionable_files": len(actionable),
            "reclaimable_mb":   total_reclaimable,
        },
        "recommended_action": (
            f"{len(actionable)} data file(s) hold ~{total_reclaimable:,.0f} MB of free space "
            "beyond the 16% safety buffer. Start with the safe reclaim (TRUNCATEONLY — drops "
            "trailing free space, no fragmentation). Only if that can't reach the target is "
            "Deep Compaction offered, which moves pages and then rebuilds affected indexes. "
            "Run during a low-traffic window; the shrink backs off (no KILL) if it hits a lock."
        ),
        "estimated_impact": f"~{total_reclaimable:,.0f} MB of disk returned to the OS after reclamation.",
        # Executable only via its own per-file panel (not the batch checkbox).
        "executable": True, "eligible_for_fix": False, "blocking_reason": None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Execute — Phase 1 (truncate_only) / Phase 3 (deep_compaction)
# ─────────────────────────────────────────────────────────────────────────────

def _find_blocker(cursor) -> Optional[int]:
    """Best-effort: a session currently blocking another (reported, never killed)."""
    try:
        cursor.execute(
            "SELECT TOP 1 blocking_session_id FROM sys.dm_exec_requests "
            "WHERE blocking_session_id <> 0"
        )
        row = cursor.fetchone()
        return int(row[0]) if row and row[0] else None
    except pyodbc.Error:
        return None


def _run_shrink(conn, logical_name: str, target: Optional[int]) -> dict:
    """
    Run one DBCC SHRINKFILE under a lock timeout. target=None → TRUNCATEONLY
    (non-fragmenting). Returns {ok, blocked, blocking_spid, error, command}.
    """
    cursor = conn.cursor()
    if target is None:
        body = "N'DBCC SHRINKFILE(' + QUOTENAME(@ln) + N', TRUNCATEONLY) WITH NO_INFOMSGS'"
        audit = f"DBCC SHRINKFILE(QUOTENAME('{logical_name}'), TRUNCATEONLY)"
    else:
        body = ("N'DBCC SHRINKFILE(' + QUOTENAME(@ln) + N', ' + CAST(@sz AS NVARCHAR(10)) + "
                "N') WITH NO_INFOMSGS'")
        audit = f"DBCC SHRINKFILE(QUOTENAME('{logical_name}'), {target})"

    # SET LOCK_TIMEOUT requires an integer LITERAL — it cannot be parameterised,
    # so the (int-validated, config-sourced) value is inlined; @ln/@sz stay bound.
    lock_ms = int(SHRINK_LOCK_TIMEOUT_MS)
    sql = (
        f"SET LOCK_TIMEOUT {lock_ms}; "
        "DECLARE @ln NVARCHAR(128) = ?; DECLARE @sz INT = ?; "
        f"DECLARE @sql NVARCHAR(MAX) = {body}; EXEC sp_executesql @sql;"
    )
    try:
        conn.autocommit = True   # DBCC requires autocommit
        cursor.execute(sql, logical_name, (target or 0))
        return {"ok": True, "blocked": False, "blocking_spid": None, "error": None, "command": audit}
    except pyodbc.Error as exc:
        msg = str(exc)
        if "1222" in msg or "Lock request time out" in msg:
            spid = _find_blocker(conn.cursor())
            logger.warning("SHRINKFILE on %s backed off on a lock (blocker SPID=%s).", logical_name, spid)
            return {"ok": False, "blocked": True, "blocking_spid": spid, "error": None, "command": audit}
        logger.error("SHRINKFILE failed on %s (details not forwarded).", logical_name, exc_info=True)
        return {"ok": False, "blocked": False, "blocking_spid": None,
                "error": "DBCC SHRINKFILE failed. Check SQL Server error logs.", "command": audit}
    finally:
        conn.autocommit = False


def _check_alter_permission(cursor) -> bool:
    try:
        cursor.execute("SELECT HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'ALTER'), IS_SRVROLEMEMBER('sysadmin')")
        has_alter, is_sysadmin = cursor.fetchone()
        return bool(has_alter or is_sysadmin)
    except pyodbc.Error:
        return True  # can't check → proceed cautiously


def _spawn_monitor(spid: int, conn_factory, report_progress, logical_name: str):
    """
    Start a daemon thread that polls a SEPARATE connection for the live shrink
    progress (so the synchronous DBCC never locks out its own telemetry) and
    publishes percent_complete / wait_type / blocking SPID via report_progress.
    Returns (stop_event, thread). No-op-safe if it can't open a monitor conn.
    """
    stop = threading.Event()

    def _poll():
        mconn, err = conn_factory()
        if err or mconn is None:
            return
        try:
            cur = mconn.cursor()
            while not stop.wait(1.0):
                try:
                    cur.execute(
                        "SELECT percent_complete, wait_type, blocking_session_id "
                        "FROM sys.dm_exec_requests "
                        "WHERE session_id = ? AND command = 'DbccFilesCompact'",
                        spid,
                    )
                    row = cur.fetchone()
                except pyodbc.Error:
                    continue
                if row:
                    report_progress({
                        "phase": "shrinking",
                        "file": logical_name,
                        "command": "DbccFilesCompact",
                        "percent_complete": round(float(row[0]), 1) if row[0] is not None else 0.0,
                        "wait_type": row[1],
                        "blocking_spid": int(row[2]) if row[2] else None,
                    })
        finally:
            try:
                mconn.close()
            except Exception:
                pass

    t = threading.Thread(target=_poll, name=f"shrink-monitor-{logical_name}", daemon=True)
    t.start()
    return stop, t


def _shrink_with_monitor(conn, logical_name, target, conn_factory, report_progress):
    """Run a shrink, attaching a live progress monitor for page-moving shrinks."""
    # TRUNCATEONLY is instant (no page moves) and has no monitorable progress.
    if target is None or not (conn_factory and report_progress):
        return _run_shrink(conn, logical_name, target)
    try:
        spid = conn.cursor().execute("SELECT @@SPID").fetchone()[0]
    except pyodbc.Error:
        spid = None
    stop = thread = None
    if spid:
        stop, thread = _spawn_monitor(int(spid), conn_factory, report_progress, logical_name)
    try:
        return _run_shrink(conn, logical_name, target)
    finally:
        if stop:
            stop.set()
        if thread:
            thread.join(timeout=3)


def execute(
    conn: pyodbc.Connection,
    recovery_choice: str | None = None,   # 'truncate_only' (default) | 'deep_compaction' | 'minimize_file'
    conn_factory: Optional[Callable[[], tuple]] = None,   # () -> (conn, err) for the monitor thread
    report_progress: Optional[Callable[[dict], None]] = None,
    **_ignored,
) -> dict:
    """
    Three explicit modes (the caller chooses; nothing auto-escalates):

      truncate_only (default) — DBCC SHRINKFILE(name, TRUNCATEONLY). Safe: drops
        trailing free space only, zero page moves, zero fragmentation. Reports
        whether deep work is still needed.

      deep_compaction — page-moving shrink to the 16% target, THEN rebuild the
        indexes the shrink fragmented (clean indexes; file regrows into the buffer).

      minimize_file — REBUILD first (compacts rows to true minimal size), THEN
        shrink to the now-accurate used/0.84 target. Smallest possible file, but
        the final shrink RE-INTRODUCES fragmentation (indexes are NOT rebuilt
        afterward, by design — see the UI warning).

    conn_factory/report_progress are optional; when supplied, page-moving shrinks
    publish live telemetry (percent_complete, blocking SPID) for the dashboard.
    """
    def emit(d):
        if report_progress:
            report_progress(d)

    cursor = conn.cursor()
    if not _check_alter_permission(cursor):
        emit({"phase": "done"})
        return {"status": "skipped", "results": [],
                "message": "Current login lacks ALTER DATABASE permission required for DBCC SHRINKFILE."}

    files = _data_files(cursor)
    actionable = [f for f in files if f["shrink_required"]]
    if not actionable:
        emit({"phase": "done"})
        return {"status": "skipped", "results": [],
                "message": "Data file layout already optimized — nothing to reclaim."}

    mode = recovery_choice or "truncate_only"
    rebuild_summary = None

    # minimize_file: rebuild FIRST so used-space is accurate/minimal before shrink.
    if mode == "minimize_file":
        emit({"phase": "rebuilding", "message": "Compacting indexes before shrink…"})
        logger.info("minimize_file: rebuilding fragmented indexes before shrink.")
        rebuild_summary = ifrag.execute(conn)
        files = _data_files(cursor)                 # re-measure on the now-clean footprint
        actionable = [f for f in files if f["shrink_required"]]

    page_move = mode in ("deep_compaction", "minimize_file")
    results = []
    success = fail = blocked = 0

    for f in actionable:
        name = f["logical_name"]
        target = f["target_mb"] if page_move else None   # None → TRUNCATEONLY
        emit({"phase": "shrinking", "file": name, "percent_complete": 0})
        r = _shrink_with_monitor(conn, name, target, conn_factory, report_progress)
        after = _data_files(cursor)
        af = next((x for x in after if x["logical_name"] == name), None)

        entry = {
            "logical_name": name,
            "phase": mode,
            "command_executed": r["command"],
            "before_metrics": {"size_mb": f["size_mb"], "used_mb": f["used_mb"], "free_mb": f["free_mb"]},
            "after_metrics": ({"size_mb": af["size_mb"], "used_mb": af["used_mb"], "free_mb": af["free_mb"]}
                              if af else None),
            "blocking_spid": r["blocking_spid"],
        }
        if r["ok"]:
            entry["status"] = "success"
            freed = round(f["size_mb"] - (af["size_mb"] if af else f["size_mb"]), 2)
            entry["freed_mb"] = freed
            # Only the safe mode advertises that deeper work remains.
            entry["deep_compaction_available"] = (mode == "truncate_only" and af is not None and af["shrink_required"])
            entry["message"] = (f"Reclaimed {freed:,.0f} MB from [{name}] via "
                                f"{'TRUNCATEONLY' if not page_move else 'page-moving shrink'}.")
            success += 1
        elif r["blocked"]:
            entry["status"] = "blocked"
            entry["message"] = (f"Shrink of [{name}] backed off after a lock wait"
                                + (f" (blocked by SPID {r['blocking_spid']})" if r["blocking_spid"] else "")
                                + ". No session was killed — retry during lower traffic.")
            blocked += 1
        else:
            entry["status"] = "failed"
            entry["message"] = r["error"] or f"Shrink of [{name}] failed."
            fail += 1
        results.append(entry)

    # deep_compaction: shrink fragmented the indexes — rebuild them now (cleanup).
    if mode == "deep_compaction" and success:
        emit({"phase": "rebuilding", "message": "Rebuilding indexes fragmented by the shrink…"})
        logger.info("deep_compaction: rebuilding fragmented indexes (post-shrink cleanup).")
        rebuild_summary = ifrag.execute(conn)

    emit({"phase": "done"})

    deep_available = any(e.get("deep_compaction_available") for e in results)
    status = "success"
    if fail or blocked:
        status = "partial" if success else ("failed" if fail else "blocked")

    parts = [f"{success} file(s) reclaimed"]
    if blocked: parts.append(f"{blocked} blocked (backed off, no kill)")
    if fail: parts.append(f"{fail} failed")
    message = "; ".join(parts) + "."
    if mode == "minimize_file":
        message += (" Indexes were compacted first, then the file shrunk to its minimal "
                    "footprint — the final shrink re-introduced some fragmentation (as warned).")
        if rebuild_summary:
            message += f" Pre-shrink rebuild: {rebuild_summary.get('message', 'done')}"
    elif rebuild_summary:
        message += f" Post-shrink index rebuild: {rebuild_summary.get('message', 'done')}"
    elif deep_available:
        message += " TRUNCATEONLY left free space beyond the buffer — Deep Compaction available (explicit confirm required)."

    return {
        "status": status,
        "message": message,
        "results": results,
        "deep_compaction_available": deep_available,
        "rebuild_summary": rebuild_summary,
        "recovery_choice": recovery_choice,
    }
