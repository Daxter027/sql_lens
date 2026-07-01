"""
stale_statistics.py
-------------------
Finds column/index statistics that are stale — either a large fraction of rows
changed since the last update, or simply not updated in a long time. Stale stats
lead the optimizer to bad row estimates and slow plans.

READ-ONLY / ANALYSIS-ONLY. It reports the stale stats with a ready
UPDATE STATISTICS script per table. (UPDATE STATISTICS is relatively safe but can
be I/O-heavy with FULLSCAN, so we script it rather than auto-run it.)
"""

from __future__ import annotations
import logging
from typing import Any
import pyodbc

logger = logging.getLogger(__name__)

ISSUE_ID   = "stale_statistics"
ISSUE_NAME = "Stale Statistics"

_MIN_ROWS = 1000          # ignore tiny tables — stale stats there rarely matter
_STALE_DAYS = 30          # not updated in this many days → stale
_MOD_FRACTION = 0.20      # >20% of rows modified since last update → stale
_MAX_DETAIL = 150         # cap per-stat detail rows in the payload (count kept accurate)
_MAX_SCRIPTS = 300        # cap UPDATE STATISTICS scripts returned

# Modern path (SQL 2008 R2 SP2 / 2012+) — accurate per-stat modification_counter.
# Columns: schema, table, stat, last_updated, rows, mods.
_SQL_MODERN = """
SELECT s.name, t.name, st.name, sp.last_updated, sp.rows, sp.modification_counter
FROM sys.stats st
JOIN sys.tables  t ON t.object_id = st.object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
CROSS APPLY sys.dm_db_stats_properties(st.object_id, st.stats_id) sp
WHERE t.is_ms_shipped = 0
  AND sp.rows >= ?
  AND (sp.last_updated IS NULL
    OR sp.last_updated < DATEADD(day, -?, GETDATE())
    OR sp.modification_counter > (sp.rows * ?))
ORDER BY sp.modification_counter DESC
"""

# Legacy path (SQL 2008 RTM, no dm_db_stats_properties) — STATS_DATE for the age
# and sysindexes.rowmodctr as an approximate modification counter.
_SQL_LEGACY = """
SELECT s.name, t.name, st.name,
       STATS_DATE(st.object_id, st.stats_id) AS last_updated,
       ps.rows, ISNULL(si.rowmodctr, 0) AS mods
FROM sys.stats st
JOIN sys.tables  t ON t.object_id = st.object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
JOIN (SELECT object_id, SUM(rows) AS rows FROM sys.partitions
      WHERE index_id IN (0, 1) GROUP BY object_id) ps ON ps.object_id = st.object_id
LEFT JOIN sys.sysindexes si ON si.id = st.object_id AND si.indid = st.stats_id
WHERE t.is_ms_shipped = 0
  AND ps.rows >= ?
  AND (STATS_DATE(st.object_id, st.stats_id) IS NULL
    OR STATS_DATE(st.object_id, st.stats_id) < DATEADD(day, -?, GETDATE())
    OR ISNULL(si.rowmodctr, 0) > (ps.rows * ?))
ORDER BY ISNULL(si.rowmodctr, 0) DESC
"""


def analyze(conn: pyodbc.Connection) -> dict[str, Any]:
    cursor = conn.cursor()
    # Prefer the accurate modern DMV; fall back to the 2008-compatible query.
    try:
        cursor.execute(_SQL_MODERN, _MIN_ROWS, _STALE_DAYS, _MOD_FRACTION)
        rows_out = cursor.fetchall()
    except pyodbc.Error:
        logger.info("stale_statistics: dm_db_stats_properties unavailable — using legacy path")
        cursor.execute(_SQL_LEGACY, _MIN_ROWS, _STALE_DAYS, _MOD_FRACTION)
        rows_out = cursor.fetchall()

    from datetime import datetime
    findings = []
    tables = set()
    for (schema, table, stat, last_updated, rows, mods) in rows_out:
        rows = int(rows or 0)
        mods = int(mods or 0)
        pct = round(mods / rows * 100, 1) if rows else 0.0
        days_old = None
        if last_updated is not None:
            try:
                days_old = (datetime.now() - last_updated).days
            except Exception:
                days_old = None
        tables.add((schema, table))
        findings.append({
            "schema": schema,
            "table": table,
            "statistic": stat,
            "last_updated": str(last_updated)[:19] if last_updated else "Never",
            "days_old": days_old,
            "rows": rows,
            "rows_modified": mods,
            "modified_pct": pct,
        })

    total = len(findings)
    # One UPDATE STATISTICS script per affected table (updates all its stats).
    scripts = [f"UPDATE STATISTICS [{sch}].[{tbl}] WITH FULLSCAN;" for (sch, tbl) in sorted(tables)]

    truncated = total > _MAX_DETAIL
    note = (
        f"Flagged when a statistic is older than {_STALE_DAYS} days or more than "
        f"{int(_MOD_FRACTION*100)}% of its rows changed since the last update "
        f"(tables under {_MIN_ROWS:,} rows are ignored). Auto-update stats only "
        "refresh at ~20% change, which can lag on large tables."
        + (f" Showing the top {_MAX_DETAIL} of {total} stale statistics." if truncated else "")
    )

    if not findings:
        return {
            "issue_id": ISSUE_ID, "issue_name": ISSUE_NAME, "severity": "Low",
            "affected_objects": [], "current_metrics": {"stale_count": 0},
            "recommended_action": "No stale statistics found.",
            "estimated_impact": "N/A", "executable": False, "eligible_for_fix": False,
            "analysis_note": note,
        }

    severity = "Medium" if total > 20 else "Low"
    return {
        "issue_id": ISSUE_ID, "issue_name": ISSUE_NAME, "severity": severity,
        "affected_objects": findings[:_MAX_DETAIL],   # cap detail payload
        "current_metrics": {
            "stale_count": total,
            "affected_tables": len(tables),
            "update_scripts": scripts[:_MAX_SCRIPTS],
        },
        "recommended_action": (
            f"Found {total} stale statistic(s) across {len(tables)} table(s). "
            "Refresh them with UPDATE STATISTICS (one script per table is provided) during a "
            "low-traffic window — FULLSCAN is most accurate but I/O-heavy on large tables."
        ),
        "estimated_impact": "Better query plans / row estimates on the affected tables.",
        "executable": False, "eligible_for_fix": False,
        "analysis_note": note,
    }
