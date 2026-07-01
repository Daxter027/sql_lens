"""
routers/storage_redundancy.py
-----------------------------
On-demand endpoint wrapping the single run_storage_redundancy_analysis()
function. Kept off the parallel /analyze batch because it makes a network call
to the Anthropic API; it runs in its own (serial) executor so the request never
starves the analysis thread pool.
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, HTTPException, Header, Query

from models import StorageRedundancyResponse
from session import store
from db import get_connection_from_session
import analysis.storage_redundancy as sr

logger = logging.getLogger(__name__)
router = APIRouter()

# Serial: one analysis at a time is plenty for an on-demand button.
_executor = ThreadPoolExecutor(max_workers=1)


@router.post("/storage-redundancy", response_model=StorageRedundancyResponse)
async def storage_redundancy(
    x_session_token: str = Header(..., alias="X-Session-Token"),
    model: str | None = Query(None, description="Override ANTHROPIC_MODEL for this run (e.g. claude-haiku-4-5)"),
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
                "total_user_table_count": 0, "analyzed_table_count": 0, "analyzed_percentage": 0,
                "was_truncated": False, "table_data": [], "analysis_markdown": None, "model_used": None,
            }
        try:
            return sr.run_storage_redundancy_analysis(conn, model=model)
        finally:
            conn.close()

    result = await loop.run_in_executor(_executor, _run)
    return StorageRedundancyResponse(**result)
