"""
routers/execute.py
------------------
Execution endpoint. Wired issues in this version:
  - transaction_log_growth  (Issue 1) — log shrink
  - heap_clustering         (Issue 2) — CREATE CLUSTERED INDEX
  - unused_indexes          (Issue 4) — ALTER INDEX ... DISABLE (reversible)
  - ghost_pages             (Issue 5) — ALTER INDEX REORGANIZE / REBUILD

Issue 3 (string_storage) is permanently blocked — automated column type
narrowing risks silent data loss and is never performed by this tool.

Any request for an un-wired issue returns HTTP 501 with a clear message.
The client is never misled about what actually ran.
"""

import logging
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import asyncio
from fastapi import APIRouter, HTTPException, Header
from models import ExecuteRequest, ExecuteResponse
from session import store
from progress import store as progress_store
from db import get_connection_from_session
import analysis.transaction_log  as tl
import analysis.heap_clustering   as hc
import analysis.unused_indexes    as ui
import analysis.ghost_pages       as gp
import analysis.blank_string_contamination as bsc
import analysis.shadow_tables              as st
import analysis.index_fragmentation        as ifrag
import analysis.data_file_reclaim          as dfr

logger = logging.getLogger(__name__)
router = APIRouter()
_executor = ThreadPoolExecutor(max_workers=2)

# Issues with a wired executable action in this version.
#   blank_string_contamination → convert blank/whitespace to NULL (reversible-ish, non-destructive)
#   shadow_tables              → QUARANTINE RENAME only (reversible); never DROP
EXECUTABLE_ISSUE_IDS = {
    "transaction_log_growth", "heap_clustering", "unused_indexes", "ghost_pages",
    "blank_string_contamination", "shadow_tables", "index_fragmentation",
    "data_file_reclaim",
}

# Issue 3 is permanently blocked regardless of version.
PERMANENTLY_BLOCKED = {"string_storage"}


@router.post("/execute", response_model=ExecuteResponse)
async def execute(req: ExecuteRequest):

    # ── Permanent block (string_storage) ─────────────────────────────────────
    if req.issue_id in PERMANENTLY_BLOCKED:
        raise HTTPException(
            status_code=501,
            detail=(
                f"Execution of '{req.issue_id}' is permanently out of scope. "
                "Automated column type narrowing risks silent data truncation and "
                "will never be performed by this tool. A DBA must apply schema "
                "changes manually after application testing and a maintenance window."
            ),
        )

    # ── Not-yet-wired issues ──────────────────────────────────────────────────
    if req.issue_id not in EXECUTABLE_ISSUE_IDS:
        raise HTTPException(
            status_code=501,
            detail=(
                f"Execution of '{req.issue_id}' is not yet implemented. "
                "Currently executable: " + ", ".join(sorted(EXECUTABLE_ISSUE_IDS)) + "."
            ),
        )

    # ── Session validation ────────────────────────────────────────────────────
    session = store.get(req.session_token)
    if not session:
        raise HTTPException(
            status_code=401,
            detail="Session not found or expired. Please reconnect."
        )

    loop = asyncio.get_event_loop()

    # ── Issue 1: Transaction log shrink ───────────────────────────────────────
    if req.issue_id == "transaction_log_growth":
        def _do_tl():
            conn, err = get_connection_from_session(session)
            if err:
                return {
                    "status":           "failed",
                    "message":          err,
                    "command_executed": None,
                    "before_metrics":   None,
                    "after_metrics":    None,
                    "delta_mb_freed":   None,
                }
            try:
                return tl.execute(conn, req.recovery_choice)
            finally:
                conn.close()

        result = await loop.run_in_executor(_executor, _do_tl)

    # ── Issue 2: Heap clustering ──────────────────────────────────────────────
    elif req.issue_id == "heap_clustering":
        def _do_hc():
            conn, err = get_connection_from_session(session)
            if err:
                return {
                    "status":           "failed",
                    "message":          err,
                    "command_executed": None,
                    "before_metrics":   None,
                    "after_metrics":    None,
                }
            try:
                return hc.execute(
                    conn,
                    target_schema=req.target_schema,
                    target_table=req.target_table,
                    target_column=req.target_column,
                )
            finally:
                conn.close()

        result = await loop.run_in_executor(_executor, _do_hc)

    # ── Issue 4: Unused index disable ─────────────────────────────────────────
    elif req.issue_id == "unused_indexes":
        def _do_ui():
            conn, err = get_connection_from_session(session)
            if err:
                return {"status": "failed", "message": err,
                        "command_executed": None, "before_metrics": None, "after_metrics": None}
            try:
                return ui.execute(
                    conn,
                    target_schema=req.target_schema,
                    target_table=req.target_table,
                    target_column=req.target_column,
                )
            finally:
                conn.close()

        result = await loop.run_in_executor(_executor, _do_ui)

    # ── Issue 5: Ghost page reconciliation ────────────────────────────────────
    elif req.issue_id == "ghost_pages":
        def _do_gp():
            conn, err = get_connection_from_session(session)
            if err:
                return {"status": "failed", "message": err,
                        "command_executed": None, "before_metrics": None, "after_metrics": None}
            try:
                return gp.execute(
                    conn,
                    target_schema=req.target_schema,
                    target_table=req.target_table,
                    target_column=req.target_column,
                )
            finally:
                conn.close()

        result = await loop.run_in_executor(_executor, _do_gp)

    # ── Fragmented index REORGANIZE / REBUILD ─────────────────────────────────
    elif req.issue_id == "index_fragmentation":
        def _do_ifrag():
            conn, err = get_connection_from_session(session)
            if err:
                return {"status": "failed", "message": err,
                        "command_executed": None, "before_metrics": None, "after_metrics": None}
            try:
                return ifrag.execute(
                    conn,
                    target_schema=req.target_schema,
                    target_table=req.target_table,
                    target_column=req.target_column,
                )
            finally:
                conn.close()

        result = await loop.run_in_executor(_executor, _do_ifrag)

    # ── Data file space reclamation (truncate_only / deep_compaction / minimize_file) ─
    elif req.issue_id == "data_file_reclaim":
        token = req.session_token

        def _report(d):
            progress_store.set(token, d)

        def _monitor_conn():
            # Fresh connection for the background progress poller (the shrink's
            # own connection is blocked synchronously).
            return get_connection_from_session(session)

        def _do_dfr():
            conn, err = get_connection_from_session(session)
            if err:
                return {"status": "failed", "message": err, "results": []}
            try:
                return dfr.execute(
                    conn,
                    recovery_choice=req.recovery_choice,
                    conn_factory=_monitor_conn,
                    report_progress=_report,
                )
            finally:
                conn.close()
                progress_store.clear(token)

        result = await loop.run_in_executor(_executor, _do_dfr)

    # ── Problem 14: Blank-string → NULL conversion ────────────────────────────
    elif req.issue_id == "blank_string_contamination":
        def _do_bsc():
            conn, err = get_connection_from_session(session)
            if err:
                return {"status": "failed", "message": err,
                        "command_executed": None, "before_metrics": None, "after_metrics": None}
            try:
                return bsc.execute(
                    conn,
                    target_schema=req.target_schema,
                    target_table=req.target_table,
                    target_column=req.target_column,
                )
            finally:
                conn.close()

        result = await loop.run_in_executor(_executor, _do_bsc)

    # ── Problem 20: Shadow-table QUARANTINE RENAME (never DROP) ────────────────
    elif req.issue_id == "shadow_tables":
        def _do_st():
            conn, err = get_connection_from_session(session)
            if err:
                return {"status": "failed", "message": err,
                        "command_executed": None, "before_metrics": None, "after_metrics": None}
            try:
                # NOTE: calls quarantine(), NOT execute() — removal is never performed.
                return st.quarantine(
                    conn,
                    target_schema=req.target_schema,
                    target_table=req.target_table,
                )
            finally:
                conn.close()

        result = await loop.run_in_executor(_executor, _do_st)

    now = datetime.now(timezone.utc).isoformat()

    response = ExecuteResponse(
        issue_id=req.issue_id,
        status=result["status"],
        command_executed=result.get("command_executed"),
        before_metrics=result.get("before_metrics"),
        after_metrics=result.get("after_metrics"),
        delta_mb_freed=result.get("delta_mb_freed"),
        results=result.get("results"),
        message=result.get("message", ""),
        executed_at=now,
        recovery_choice=req.recovery_choice,
        deep_compaction_available=result.get("deep_compaction_available"),
        rebuild_summary=result.get("rebuild_summary"),
    )

    # Cache for report endpoint. Keep a running list so a batch of optimizations
    # all appear in the final report (last_execution kept for back-compat).
    dumped = response.model_dump()
    session.last_execution = dumped
    session.last_executions.append(dumped)
    return response


@router.get("/reclaim-progress")
async def reclaim_progress(x_session_token: str = Header(..., alias="X-Session-Token")):
    """Live telemetry for an in-flight data-file reclamation (polled by the UI).
    Returns {} when nothing is running. Read-only and cheap."""
    return progress_store.get(x_session_token)

