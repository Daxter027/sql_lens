"""
adhoc_plan_cache.py
-------------------
"Ad-Hoc Workload & Plan Cache Analyzer" — a metadata-only look at the instance
plan cache for single-use, non-parameterized (Adhoc/Prepared) query bloat.

READ-ONLY / ANALYSIS-ONLY (zero-execution). It reports the wasted cache and
provides two copy/export-ready remediation scripts; it never runs them.

SQL Server 2008 safe: sys.dm_exec_cached_plans exists since 2005. NOTE: the plan
cache is INSTANCE-WIDE, so the figures reflect the whole server, not just the
connected database. Requires VIEW SERVER STATE (handled gracefully if absent).
"""

from __future__ import annotations
import logging
from typing import Any
import pyodbc

logger = logging.getLogger(__name__)

ISSUE_ID   = "adhoc_plan_cache"
ISSUE_NAME = "Ad-Hoc Workload Analyzer"

_SQL = """
SELECT
    objtype AS CachePlanType,
    COUNT_BIG(*) AS TotalSingleUsePlans,
    CAST(SUM(size_in_bytes) / 1024.0 / 1024.0 AS DECIMAL(10,2)) AS WastedCacheMB
FROM sys.dm_exec_cached_plans
WHERE cacheobjtype = 'Compiled Plan'
  AND objtype IN ('Adhoc', 'Prepared')
  AND usecounts = 1
GROUP BY objtype
"""

# Two non-auto-executing remediation options, verbatim-commented for copy/export.
_REMEDIATION = [
    {
        "title": "Option A — Enable Server-Level Throttle (Recommended)",
        "script": (
            "-- OPTION A: Enable Server-Level Throttle (Recommended)\n"
            "-- Tells SQL Server 2008 to store a tiny 16-byte stub on the first execution\n"
            "-- instead of a full plan, completely stopping ad-hoc cache bloat.\n"
            "EXEC sp_configure 'show advanced options', 1;\n"
            "RECONFIGURE;\n"
            "EXEC sp_configure 'optimize for ad hoc workloads', 1;\n"
            "RECONFIGURE;"
        ),
    },
    {
        "title": "Option B — Emergency Cache Flush (Maintenance Window Only)",
        "script": (
            "-- OPTION B: Emergency Cache Flush (Maintenance Window Only)\n"
            "-- Instantly clears out the current wasted plan memory footprint.\n"
            "-- Run during off-peak hours as it forces compilation on subsequent unique queries.\n"
            "DBCC FREEPROCCACHE;"
        ),
    },
]

_NOTE = ("The plan cache is instance-wide, so these figures reflect the whole SQL Server "
         "instance, not just the connected database.")


def analyze(conn: pyodbc.Connection) -> dict[str, Any]:
    cursor = conn.cursor()
    try:
        cursor.execute(_SQL)
        rows = cursor.fetchall()
    except pyodbc.Error:
        logger.info("adhoc_plan_cache: plan-cache query failed (needs VIEW SERVER STATE)", exc_info=True)
        return {
            "issue_id": ISSUE_ID, "issue_name": ISSUE_NAME, "severity": "Low",
            "affected_objects": [], "current_metrics": {},
            "recommended_action": "Could not read the plan cache — the login needs the VIEW SERVER STATE permission.",
            "estimated_impact": "N/A", "executable": False, "eligible_for_fix": False,
            "analysis_note": _NOTE,
        }

    findings, total_plans, wasted_mb = [], 0, 0.0
    for (plan_type, cnt, mb) in rows:
        cnt = int(cnt or 0)
        mb = float(mb or 0)
        total_plans += cnt
        wasted_mb += mb
        findings.append({"plan_type": plan_type, "single_use_plans": cnt, "wasted_mb": round(mb, 2)})
    wasted_mb = round(wasted_mb, 2)

    if total_plans == 0:
        return {
            "issue_id": ISSUE_ID, "issue_name": ISSUE_NAME, "severity": "Low",
            "affected_objects": [],
            "current_metrics": {"total_single_use_plans": 0, "wasted_cache_mb": 0},
            "recommended_action": "No single-use ad-hoc/prepared plan bloat detected in the cache.",
            "estimated_impact": "N/A", "executable": False, "eligible_for_fix": False,
            "analysis_note": _NOTE,
        }

    severity = "High" if wasted_mb > 1000 else "Medium" if wasted_mb > 200 else "Low"
    return {
        "issue_id": ISSUE_ID, "issue_name": ISSUE_NAME, "severity": severity,
        "affected_objects": findings,
        "current_metrics": {
            "total_single_use_plans": total_plans,
            "wasted_cache_mb": wasted_mb,
            "remediation_scripts": _REMEDIATION,
        },
        "recommended_action": (
            f"Found {total_plans:,} single-use query plans wasting {wasted_mb:,.2f} MB of server memory assets."
        ),
        "estimated_impact": (
            f"~{wasted_mb:.0f} MB of buffer pool reclaimable for data caching; fewer recompiles under load."
        ),
        "executable": False, "eligible_for_fix": False,
        "analysis_note": _NOTE,
    }
