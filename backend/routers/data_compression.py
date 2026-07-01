"""
routers/data_compression.py
---------------------------
On-demand endpoint for compression-savings estimation. Kept off the /analyze
batch because sp_estimate_data_compression_savings samples data into tempdb and
is slow on large tables. Serial executor so only one estimate runs at a time.
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, HTTPException, Header, Query

from models import DataCompressionResponse
from session import store
from db import get_connection_from_session
import analysis.data_compression as dc

logger = logging.getLogger(__name__)
router = APIRouter()

_executor = ThreadPoolExecutor(max_workers=1)


@router.post("/data-compression", response_model=DataCompressionResponse)
async def data_compression(
    x_session_token: str = Header(..., alias="X-Session-Token"),
    top_n: int = Query(25, ge=1, le=100, description="How many of the largest tables to estimate"),
    mode: str = Query("PAGE", description="PAGE or ROW compression"),
):
    session = store.get(x_session_token)
    if not session:
        raise HTTPException(status_code=401, detail="Session not found or expired. Please reconnect.")

    loop = asyncio.get_event_loop()

    def _run():
        conn, err = get_connection_from_session(session)
        if err:
            return {"status": "error", "error_kind": "db_error", "error": err, "message": err,
                    "mode": mode, "analyzed_table_count": 0, "tables": [],
                    "total_current_mb": 0, "total_compressed_mb": 0, "total_savings_mb": 0,
                    "total_savings_pct": 0}
        try:
            return dc.run_data_compression_analysis(conn, top_n=top_n, mode=mode)
        finally:
            conn.close()

    result = await loop.run_in_executor(_executor, _run)
    return DataCompressionResponse(**result)
