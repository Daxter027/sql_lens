"""
routers/analyze.py
------------------
Runs the diagnostic checks against the connected database.
All checks are read-only — no data, schema, or settings are touched.

The endpoint supports running a SUBSET of checks via the optional `checks`
query param (comma-separated issue ids). The frontend uses this to lazy-load:
it requests the four fast checks first so the screen paints quickly, then
requests the slow `ghost_pages` check separately. Subset results are merged
into the cached analysis so the report always reflects the full picture.
"""

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from fastapi import APIRouter, HTTPException, Header, Query, Request
from models import AnalyzeResponse
from session import store
from db import get_connection_from_session
import analysis.transaction_log as tl
import analysis.heap_clustering  as hc
import analysis.string_storage   as ss
import analysis.unused_indexes   as ui
import analysis.ghost_pages      as gp
import analysis.blank_string_contamination as bsc
import analysis.shadow_tables              as st
import analysis.inappropriate_datatypes    as idt
import analysis.archival_candidates        as ac
import analysis.index_fragmentation        as ifrag
import analysis.data_file_reclaim          as dfr
import analysis.missing_indexes            as mi
import analysis.stale_statistics           as sstat
import analysis.duplicate_indexes          as di
import analysis.security_audit             as sec
import analysis.adhoc_plan_cache           as apc

logger = logging.getLogger(__name__)
router = APIRouter()
_executor = ThreadPoolExecutor(max_workers=16)

# Canonical issue id → analyze fn, in display order (Phase 1: 1-5, Phase 2: 6+).
CHECKS = [
    ("transaction_log_growth",     tl.analyze),
    ("heap_clustering",            hc.analyze),
    ("string_storage",             ss.analyze),
    ("unused_indexes",             ui.analyze),
    ("ghost_pages",                gp.analyze),
    ("index_fragmentation",        ifrag.analyze),
    ("blank_string_contamination", bsc.analyze),
    ("shadow_tables",              st.analyze),
    ("inappropriate_datatypes",    idt.analyze),
    ("archival_candidates",        ac.analyze),
    ("data_file_reclaim",          dfr.analyze),
    ("missing_indexes",            mi.analyze),
    ("stale_statistics",           sstat.analyze),
    ("duplicate_indexes",          di.analyze),
    ("security_audit",             sec.analyze),
    ("adhoc_plan_cache",           apc.analyze),
]
CHECK_ORDER = [cid for cid, _ in CHECKS]


def _run_check(issue_id, module_analyze_fn, conn):
    """Run a single analysis function, catching any exception gracefully."""
    try:
        return module_analyze_fn(conn)
    except Exception as exc:
        logger.error("Analysis check failed: %s — %s", issue_id, exc, exc_info=True)
        return {
            "issue_id":         issue_id,
            "issue_name":       "Check failed",
            "severity":         "Low",
            "affected_objects": [],
            "current_metrics":  {},
            "recommended_action": "Analysis encountered an error.",
            "estimated_impact": "N/A",
            "executable":       False,
            "eligible_for_fix": False,
            "error":            "Analysis check encountered an internal error.",
        }


@router.post("/analyze", response_model=AnalyzeResponse)
async def analyze(
    request: Request,
    x_session_token: str = Header(..., alias="X-Session-Token"),
    checks: str | None = Query(
        None,
        description="Comma-separated issue ids to run. Omit to run all checks.",
    ),
):
    session = store.get(x_session_token)
    if not session:
        raise HTTPException(status_code=401, detail="Session not found or expired. Please reconnect.")

    # Resolve which checks to run (default: all), preserving canonical order.
    requested = {c.strip() for c in checks.split(",")} if checks else set(CHECK_ORDER)
    unknown = requested - set(CHECK_ORDER)
    if unknown:
        logger.warning("Unknown check IDs requested (ignored): %s", unknown)
    selected = [(cid, fn) for cid, fn in CHECKS if cid in requested]
    if not selected:
        raise HTTPException(status_code=400, detail="No valid checks requested.")

    loop = asyncio.get_event_loop()

    # Track active (conn, spid) pairs so we can KILL them on disconnect.
    # KILL is the only reliable way to abort an in-flight SQL Server query —
    # closing the pyodbc connection from another thread is not thread-safe and
    # does not guarantee the server-side query stops.
    _lock = threading.Lock()
    _active: list[tuple] = []   # [(conn, spid), ...]

    def _register(conn, spid):
        with _lock:
            _active.append((conn, spid))

    def _deregister(conn):
        with _lock:
            for i, (c, _) in enumerate(_active):
                if c is conn:
                    del _active[i]
                    return

    def _kill_all():
        with _lock:
            spids = [spid for _, spid in _active]
        if not spids:
            return
        kill_conn, err = get_connection_from_session(session)
        if err:
            logger.warning("[analyze] Could not open kill connection: %s", err)
            return
        try:
            cur = kill_conn.cursor()
            for spid in spids:
                try:
                    cur.execute(f"KILL {int(spid)}")
                    logger.info("[analyze] Killed SPID %d", spid)
                except Exception as exc:
                    logger.warning("[analyze] KILL %d failed: %s", spid, exc)
        finally:
            kill_conn.close()

    # Each check runs concurrently in its own thread with its own fresh
    # connection. Running them in parallel makes total latency ≈ the slowest
    # selected check. Separate connections avoid cursor-state conflicts.
    def run_one(issue_id, check_fn):
        # Timing is logged per check so the slowest one is obvious in the
        # server console — connect time and query time are reported separately
        # to distinguish network/auth overhead from the actual query cost.
        t0 = time.perf_counter()
        conn, err = get_connection_from_session(session)
        t_conn = time.perf_counter() - t0
        if err:
            logger.warning("[analyze] %-18s connection failed after %.2fs", issue_id, t_conn)
            return {
                "issue_id":         issue_id,
                "issue_name":       "Connection failed",
                "severity":         "Low",
                "affected_objects": [],
                "current_metrics":  {},
                "recommended_action": err,
                "estimated_impact": "N/A",
                "executable":       False,
                "eligible_for_fix": False,
                "error":            err,
            }
        try:
            spid = conn.cursor().execute("SELECT @@SPID").fetchone()[0]
        except Exception:
            spid = None
        if spid:
            _register(conn, spid)
        try:
            return _run_check(issue_id, check_fn, conn)
        finally:
            _deregister(conn)
            conn.close()
            total = time.perf_counter() - t0
            logger.info("[analyze] %-18s %.2fs (connect %.2fs, query %.2fs)",
                        issue_id, total, t_conn, total - t_conn)

    async def _wait_for_disconnect():
        # Awaiting request.receive() blocks until uvicorn pushes an
        # http.disconnect event — reliable where polling is_disconnected() is not.
        while True:
            message = await request.receive()
            if message["type"] == "http.disconnect":
                return

    t_start = time.perf_counter()
    gather_task = asyncio.ensure_future(asyncio.gather(
        *(loop.run_in_executor(_executor, run_one, cid, fn) for cid, fn in selected)
    ))
    disconnect_task = asyncio.ensure_future(_wait_for_disconnect())
    done, _ = await asyncio.wait(
        [disconnect_task, gather_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

    if disconnect_task in done:
        logger.info("[analyze] Client disconnected — killing in-flight queries")
        _kill_all()
        gather_task.cancel()
        raise HTTPException(status_code=499, detail="Client disconnected.")

    disconnect_task.cancel()
    issues = list(gather_task.result())
    logger.info("[analyze] %d check(s) [%s] completed in %.2fs (wall clock)",
                len(selected), ",".join(cid for cid, _ in selected),
                time.perf_counter() - t_start)

    response = AnalyzeResponse(
        session_token=x_session_token,
        database=session.database,
        issues=issues,
        analysed_at=datetime.now(timezone.utc).isoformat(),
    )

    # Merge into the cached analysis (by issue id, in canonical order) so a
    # subset request never clobbers previously-fetched checks and the report
    # always has the complete five-check picture.
    cached_issues = (session.last_analysis or {}).get("issues", [])
    by_id = {i.get("issue_id"): i for i in cached_issues}
    for iss in issues:
        by_id[iss.get("issue_id")] = iss
    merged = response.model_dump()
    merged["issues"] = [by_id[cid] for cid in CHECK_ORDER if cid in by_id]
    session.last_analysis = merged

    return response
