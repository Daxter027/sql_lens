"""
routers/table_intelligence.py
-----------------------------
On-demand endpoint wrapping run_table_intelligence(). Kept off the parallel
/analyze batch and given its own (serial) executor: it profiles every user table
and optionally scans the ReportServer database, so it is heavier than a single
check and better run on demand from its tile.
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, HTTPException, Header, Query

from models import TableIntelligenceResponse
from session import store
from db import get_connection_from_session
import analysis.table_intelligence as ti

logger = logging.getLogger(__name__)
router = APIRouter()

_executor = ThreadPoolExecutor(max_workers=1)


@router.post("/table-intelligence", response_model=TableIntelligenceResponse)
async def table_intelligence(
    x_session_token: str = Header(..., alias="X-Session-Token"),
    include_ssrs: bool = Query(True, description="Scan ReportServer for report→table usage"),
):
    session = store.get(x_session_token)
    if not session:
        raise HTTPException(status_code=401, detail="Session not found or expired. Please reconnect.")

    loop = asyncio.get_event_loop()

    def _run():
        conn, err = get_connection_from_session(session)
        if err:
            return {
                "status": "error", "error_kind": "db_error", "error": err, "message": err,
                "total_tables": 0, "server_start_time": None, "ssrs_available": False,
                "ssrs_report_count": 0, "ssrs_note": None, "tables": [],
            }
        try:
            return ti.run_table_intelligence(conn, include_ssrs=include_ssrs)
        finally:
            conn.close()

    result = await loop.run_in_executor(_executor, _run)
    return TableIntelligenceResponse(**result)
