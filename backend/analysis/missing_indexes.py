"""
missing_indexes.py
------------------
Surfaces SQL Server's OWN missing-index recommendations from the
sys.dm_db_missing_index_* DMVs, ranked by an impact score, with a ready-to-use
CREATE INDEX script per suggestion.

READ-ONLY / ANALYSIS-ONLY. It never creates indexes — the DMV suggestions are
heuristic (naive column order, no dedup, can over-suggest), so index creation is
a deliberate DBA decision. We rank, script, and warn; we do not auto-apply.

Caveat surfaced in the note: these DMVs RESET ON RESTART, so on a recently
restarted instance the list is thin and low-confidence.
"""

from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Any
import pyodbc

logger = logging.getLogger(__name__)

ISSUE_ID   = "missing_indexes"
ISSUE_NAME = "Missing Index Recommendations"

# Only surface suggestions with a meaningful benefit; SQL Server emits a lot of
# marginal ones. impact_score ≈ avg_cost × avg_impact% × (seeks+scans).
_MIN_IMPACT_SCORE = 1000.0
_TOP_N = 50

_SQL = """
SELECT TOP (?)
    s.name  AS schema_name,
    t.name  AS table_name,
    CAST(ROUND(migs.avg_total_user_cost * migs.avg_user_impact
               * (migs.user_seeks + migs.user_scans), 0) AS bigint) AS impact_score,
    migs.user_seeks + migs.user_scans AS uses,
    CAST(migs.avg_user_impact AS int)  AS avg_impact_pct,
    migs.last_user_seek,
    mid.equality_columns,
    mid.inequality_columns,
    mid.included_columns
FROM sys.dm_db_missing_index_group_stats migs
JOIN sys.dm_db_missing_index_groups  mig ON migs.group_handle = mig.index_group_handle
JOIN sys.dm_db_missing_index_details mid ON mig.index_handle  = mid.index_handle
JOIN sys.tables  t ON t.object_id = mid.object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
WHERE mid.database_id = DB_ID()
  AND t.is_ms_shipped = 0
ORDER BY impact_score DESC
"""


def _clean_cols(raw: str | None) -> str:
    """Strip the [brackets] the DMV wraps columns in, for a compact display."""
    return (raw or "").replace("[", "").replace("]", "")


def _build_create_index(schema: str, table: str, eq: str | None,
                        ineq: str | None, incl: str | None) -> str:
    key_cols = ", ".join(c for c in [eq, ineq] if c)      # already bracketed by DMV
    include = f" INCLUDE ({incl})" if incl else ""
    # A stable, human-readable suggested name from the key columns.
    tag = _clean_cols(eq or ineq or "cols").replace(", ", "_").replace(" ", "")[:40]
    name = f"IX_{table}_{tag}"
    return f"CREATE NONCLUSTERED INDEX [{name}] ON [{schema}].[{table}] ({key_cols}){include};"


def analyze(conn: pyodbc.Connection) -> dict[str, Any]:
    cursor = conn.cursor()

    # Restart window — DMVs are cleared on restart, so flag confidence.
    cursor.execute("SELECT sqlserver_start_time FROM sys.dm_os_sys_info")
    row = cursor.fetchone()
    restart = row[0] if row else None
    if restart and restart.tzinfo is None:
        restart = restart.replace(tzinfo=timezone.utc)
    days_up = (datetime.now(timezone.utc) - restart).days if restart else None
    low_conf = days_up is not None and days_up < 3

    cursor.execute(_SQL, _TOP_N)
    findings = []
    for (schema, table, impact, uses, impact_pct, last_seek, eq, ineq, incl) in cursor.fetchall():
        if impact is None or impact < _MIN_IMPACT_SCORE:
            continue
        findings.append({
            "schema": schema,
            "table": table,
            "impact_score": int(impact),
            "uses": int(uses or 0),
            "avg_impact_pct": int(impact_pct or 0),
            "last_used": str(last_seek)[:19] if last_seek else "—",
            "key_columns": _clean_cols(", ".join(c for c in [eq, ineq] if c)),
            "included_columns": _clean_cols(incl),
            "create_script": _build_create_index(schema, table, eq, ineq, incl),
        })

    note = (
        (f"⚠ Instance restarted {days_up} day(s) ago — these DMVs reset on restart, "
         "so the list is thin and LOW confidence. "
         if low_conf else
         f"Based on query activity since the last restart ({days_up} day(s) ago). ")
        + "These are SQL Server's raw suggestions: heuristic, not deduplicated, and column "
          "order is naive. Review each before creating — every added index has a write/space "
          "cost. Do NOT bulk-create them."
    )

    if not findings:
        return {
            "issue_id": ISSUE_ID, "issue_name": ISSUE_NAME, "severity": "Low",
            "affected_objects": [], "current_metrics": {"suggestion_count": 0},
            "recommended_action": "No high-impact missing indexes were recommended.",
            "estimated_impact": "N/A", "executable": False, "eligible_for_fix": False,
            "analysis_note": note,
        }

    top = max(f["impact_score"] for f in findings)
    severity = "High" if top > 1_000_000 else "Medium" if top > 50_000 else "Low"
    return {
        "issue_id": ISSUE_ID, "issue_name": ISSUE_NAME, "severity": severity,
        "affected_objects": findings,
        "current_metrics": {
            "suggestion_count": len(findings),
            "top_impact_score": top,
            "confidence": "LOW" if low_conf else "HIGH",
        },
        "recommended_action": (
            f"SQL Server suggests {len(findings)} index(es) that could reduce query cost. "
            "Each row includes a ready CREATE INDEX script — apply selectively in a "
            "maintenance window, favouring the highest impact scores, and avoid near-duplicates."
        ),
        "estimated_impact": "Faster reads on the affected queries; added write/storage cost per index.",
        "executable": False, "eligible_for_fix": False,
        "analysis_note": note,
    }
