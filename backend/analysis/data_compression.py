"""
data_compression.py
-------------------
Estimates ROW/PAGE data-compression savings for the largest tables using SQL
Server's own sp_estimate_data_compression_savings, and provides a ready ALTER
script per table. PAGE compression commonly reclaims 40-70% on large tables.

ON-DEMAND (its own endpoint), NOT part of the /analyze batch: the estimate proc
SAMPLES ~5% of each table into tempdb, so it is I/O-heavy and slow on multi-GB
tables. We cap it to the top-N largest tables (where the savings are anyway).

ANALYSIS-ONLY: it estimates + scripts, it never rebuilds/compresses (applying is
Enterprise-only pre-2016 SP1 and a heavy, locking operation — a DBA decision).
"""

from __future__ import annotations
import logging
from typing import Any, Optional
import pyodbc

logger = logging.getLogger(__name__)

_DEFAULT_TOP_N = 25
_MAX_TOP_N = 100

# Largest user tables by total reserved footprint (data + all indexes).
_TOP_TABLES_SQL = """
SELECT TOP (?)
    s.name AS schema_name,
    t.name AS table_name,
    CAST(ROUND(SUM(ps.reserved_page_count) * 8 / 1024.0, 2) AS decimal(18,2)) AS mb
FROM sys.tables t
JOIN sys.schemas s ON s.schema_id = t.schema_id
JOIN sys.dm_db_partition_stats ps ON ps.object_id = t.object_id
WHERE t.is_ms_shipped = 0
GROUP BY s.name, t.name
HAVING SUM(ps.reserved_page_count) > 0
ORDER BY mb DESC
"""


def _apply_script(schema: str, table: str, mode: str) -> str:
    return (f"ALTER TABLE [{schema}].[{table}] REBUILD WITH (DATA_COMPRESSION = {mode});\n"
            f"ALTER INDEX ALL ON [{schema}].[{table}] REBUILD WITH (DATA_COMPRESSION = {mode});")


def _estimate_one(cursor, schema: str, table: str, mode: str) -> Optional[tuple[float, float]]:
    """Return (current_kb, requested_kb) summed over all indexes/partitions, or None."""
    try:
        cursor.execute(
            "EXEC sp_estimate_data_compression_savings ?, ?, NULL, NULL, ?", schema, table, mode)
        cols = [d[0].lower() for d in cursor.description]
        ci = next(i for i, c in enumerate(cols) if c.startswith("size_with_current"))
        ri = next(i for i, c in enumerate(cols) if c.startswith("size_with_requested"))
        cur_kb = req_kb = 0.0
        for row in cursor.fetchall():
            cur_kb += float(row[ci] or 0)
            req_kb += float(row[ri] or 0)
        return cur_kb, req_kb
    except pyodbc.Error:
        logger.info("data_compression: estimate failed for %s.%s", schema, table, exc_info=True)
        return None


def run_data_compression_analysis(
    conn: pyodbc.Connection,
    top_n: int = _DEFAULT_TOP_N,
    mode: str = "PAGE",
) -> dict[str, Any]:
    """Estimate compression for the top-N largest tables. Read-only."""
    mode = "ROW" if str(mode).upper() == "ROW" else "PAGE"
    top_n = max(1, min(_MAX_TOP_N, int(top_n or _DEFAULT_TOP_N)))

    try:
        cursor = conn.cursor()
        cursor.execute(_TOP_TABLES_SQL, top_n)
        targets = [(r[0], r[1], float(r[2] or 0)) for r in cursor.fetchall()]
    except pyodbc.Error:
        logger.error("data_compression: top-tables query failed", exc_info=True)
        return {"status": "error", "error_kind": "db_error",
                "error": "Failed to query table sizes.", "message": "Database query failed.",
                "mode": mode, "analyzed_table_count": 0, "tables": [],
                "total_current_mb": 0, "total_compressed_mb": 0, "total_savings_mb": 0,
                "total_savings_pct": 0}

    if not targets:
        return {"status": "empty", "message": "No user tables with data were found.",
                "mode": mode, "analyzed_table_count": 0, "tables": [],
                "total_current_mb": 0, "total_compressed_mb": 0, "total_savings_mb": 0,
                "total_savings_pct": 0}

    tables = []
    tot_cur = tot_req = 0.0
    for schema, table, _mb in targets:
        est = _estimate_one(cursor, schema, table, mode)
        if est is None:
            continue
        cur_mb = round(est[0] / 1024.0, 2)
        req_mb = round(est[1] / 1024.0, 2)
        save_mb = round(cur_mb - req_mb, 2)
        tot_cur += cur_mb
        tot_req += req_mb
        tables.append({
            "schema": schema, "table": table,
            "current_mb": cur_mb, "compressed_mb": req_mb,
            "savings_mb": save_mb,
            "savings_pct": round(save_mb / cur_mb * 100, 1) if cur_mb else 0.0,
            "apply_script": _apply_script(schema, table, mode),
        })

    tables.sort(key=lambda t: t["savings_mb"], reverse=True)
    tot_save = round(tot_cur - tot_req, 2)
    return {
        "status": "ok",
        "mode": mode,
        "analyzed_table_count": len(tables),
        "tables": tables,
        "total_current_mb": round(tot_cur, 2),
        "total_compressed_mb": round(tot_req, 2),
        "total_savings_mb": tot_save,
        "total_savings_pct": round(tot_save / tot_cur * 100, 1) if tot_cur else 0.0,
        "message": "ok",
    }
