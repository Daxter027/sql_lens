"""
routers/report.py
-----------------
Returns the full database health report — analysis of all 5 issues plus
any execution result — so nothing found in the analysis is hidden or lost.
"""

import logging
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Header
from models import ReportResponse
from session import store

logger = logging.getLogger(__name__)
router = APIRouter()

@router.get("/report", response_model=ReportResponse)
async def report(x_session_token: str = Header(..., alias="X-Session-Token")):
    session = store.get(x_session_token)
    if not session:
        raise HTTPException(status_code=401, detail="Session not found or expired.")

    if not session.last_analysis:
        raise HTTPException(
            status_code=404,
            detail="No analysis results found for this session. Run /analyze first."
        )

    analysis = session.last_analysis
    executions = session.last_executions

    # Issues that were actually executed this session (by issue_id).
    executed_ids = {e.get("issue_id") for e in executions}

    # Everything not remediated this session — kept so the full health picture
    # is preserved in the report.
    all_issues = analysis.get("issues", [])
    unexecuted = [iss for iss in all_issues if iss.get("issue_id") not in executed_ids]

    return ReportResponse(
        database=session.database,
        server=session.server,
        generated_at=datetime.now(timezone.utc).isoformat(),
        analysis=analysis,
        execution=session.last_execution,
        executions=executions,
        unexecuted_issues=unexecuted,
    )
