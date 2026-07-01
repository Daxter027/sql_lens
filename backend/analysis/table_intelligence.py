"""
table_intelligence.py
---------------------
"Table Intelligence" — a per-table profile for EVERY user table in the database,
built to answer "is this table safe to archive / drop / ignore?" at a glance.

READ-ONLY. One native metadata query (point-in-time) plus an optional SSRS
report-usage pass against the ReportServer database on the same instance.

SINGLE PUBLIC ENTRY POINT: run_table_intelligence(conn).

What each column means / its limits (be honest about these in the UI):
  - created / schema_modified : sys.tables.create_date / modify_date. modify_date
        tracks DDL (schema) changes, NOT data edits.
  - last_write / last_read + writes/reads : from sys.dm_db_index_usage_stats,
        which RESETS ON SQL SERVER RESTART. So these reflect activity only since
        the last restart (see server_start_time), not lifetime history.
  - ref_total (+ by type) : count of SQL modules (procs/views/functions/triggers)
        that reference the table, via sys.sql_expression_dependencies. This is the
        "blast radius" if you change/drop it. It does NOT see external application
        code or dynamic SQL — those are invisible to SQL Server.
  - report_count : SSRS reports whose query text mentions the table name
        (heuristic, instance-wide; see _match_reports). 0 / null if SSRS skipped.

Growth over time (1d/1W/1M) is intentionally NOT here: SQL Server keeps no
history of row counts, so it cannot be computed retroactively (would require
snapshotting counts over time).
"""

from __future__ import annotations
import logging
import re
from typing import Any, Callable, Optional

import pyodbc

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Native per-table metrics — ONE query pass over all user tables
# ─────────────────────────────────────────────────────────────────────────────

_METRICS_SQL = """
WITH size_stats AS (
    SELECT ps.object_id,
           SUM(ps.row_count) AS row_count,
           CAST(ROUND(SUM(ps.reserved_page_count) * 8 / 1024.0, 2) AS decimal(18,2)) AS total_mb
    FROM sys.dm_db_partition_stats ps
    WHERE ps.index_id IN (0, 1)
    GROUP BY ps.object_id
),
idx_stats AS (
    SELECT object_id,
           SUM(CASE WHEN index_id > 0 THEN 1 ELSE 0 END) AS index_count,
           MAX(CASE WHEN index_id = 1 THEN 1 ELSE 0 END) AS has_clustered,
           MAX(CASE WHEN is_primary_key = 1 THEN 1 ELSE 0 END) AS has_pk
    FROM sys.indexes
    GROUP BY object_id
),
col_stats AS (
    SELECT object_id, COUNT(*) AS column_count FROM sys.columns GROUP BY object_id
),
trig_stats AS (
    SELECT parent_id AS object_id, COUNT(*) AS trigger_count FROM sys.triggers GROUP BY parent_id
),
fko AS (
    SELECT parent_object_id AS object_id, COUNT(*) AS fk_out FROM sys.foreign_keys GROUP BY parent_object_id
),
fki AS (
    SELECT referenced_object_id AS object_id, COUNT(*) AS fk_in FROM sys.foreign_keys GROUP BY referenced_object_id
),
dep_stats AS (
    SELECT d.referenced_id AS object_id,
           COUNT(DISTINCT d.referencing_id) AS ref_total,
           COUNT(DISTINCT CASE WHEN o.type IN ('P', 'PC') THEN d.referencing_id END) AS ref_procs,
           COUNT(DISTINCT CASE WHEN o.type = 'V' THEN d.referencing_id END) AS ref_views,
           COUNT(DISTINCT CASE WHEN o.type IN ('FN', 'IF', 'TF', 'FS', 'FT') THEN d.referencing_id END) AS ref_funcs,
           COUNT(DISTINCT CASE WHEN o.type = 'TR' THEN d.referencing_id END) AS ref_triggers
    FROM sys.sql_expression_dependencies d
    JOIN sys.objects o ON o.object_id = d.referencing_id
    WHERE d.referenced_id IS NOT NULL
    GROUP BY d.referenced_id
),
usage AS (
    SELECT object_id,
           MAX(last_user_update) AS last_write,
           MAX(lr) AS last_read,
           SUM(user_updates) AS writes,
           SUM(user_seeks + user_scans + user_lookups) AS reads
    FROM (
        SELECT object_id, last_user_update, user_updates, user_seeks, user_scans, user_lookups,
               (SELECT MAX(v) FROM (VALUES (last_user_seek), (last_user_scan), (last_user_lookup)) AS x(v)) AS lr
        FROM sys.dm_db_index_usage_stats
        WHERE database_id = DB_ID()
    ) u
    GROUP BY object_id
)
SELECT
    s.name AS SchemaName,
    t.name AS TableName,
    CONVERT(varchar(19), t.create_date, 120) AS Created,
    CONVERT(varchar(19), t.modify_date, 120) AS SchemaModified,
    ISNULL(ss.row_count, 0) AS [RowCount],
    ISNULL(ss.total_mb, 0)  AS TotalMB,
    ISNULL(cs.column_count, 0) AS ColumnCount,
    ISNULL(ix.index_count, 0)  AS IndexCount,
    CASE WHEN ISNULL(ix.has_clustered, 0) = 1 THEN 0 ELSE 1 END AS IsHeap,
    ISNULL(ix.has_pk, 0)       AS HasPK,
    ISNULL(tg.trigger_count, 0) AS TriggerCount,
    ISNULL(fo.fk_out, 0) AS FkOut,
    ISNULL(fi.fk_in, 0)  AS FkIn,
    ISNULL(dp.ref_total, 0)    AS RefTotal,
    ISNULL(dp.ref_procs, 0)    AS RefProcs,
    ISNULL(dp.ref_views, 0)    AS RefViews,
    ISNULL(dp.ref_funcs, 0)    AS RefFuncs,
    ISNULL(dp.ref_triggers, 0) AS RefTriggers,
    CONVERT(varchar(19), ug.last_write, 120) AS LastWrite,
    CONVERT(varchar(19), ug.last_read, 120)  AS LastRead,
    ISNULL(ug.writes, 0) AS Writes,
    ISNULL(ug.reads, 0)  AS Reads
FROM sys.tables t
JOIN sys.schemas s ON s.schema_id = t.schema_id
LEFT JOIN size_stats ss ON ss.object_id = t.object_id
LEFT JOIN idx_stats  ix ON ix.object_id = t.object_id
LEFT JOIN col_stats  cs ON cs.object_id = t.object_id
LEFT JOIN trig_stats tg ON tg.object_id = t.object_id
LEFT JOIN fko fo ON fo.object_id = t.object_id
LEFT JOIN fki fi ON fi.object_id = t.object_id
LEFT JOIN dep_stats dp ON dp.object_id = t.object_id
LEFT JOIN usage ug ON ug.object_id = t.object_id
WHERE t.is_ms_shipped = 0
ORDER BY ISNULL(ss.total_mb, 0) DESC
"""

_INT_COLS = ("RowCount", "ColumnCount", "IndexCount", "IsHeap", "HasPK",
             "TriggerCount", "FkOut", "FkIn", "RefTotal", "RefProcs",
             "RefViews", "RefFuncs", "RefTriggers", "Writes", "Reads")


def _row_to_dict(cols: list[str], row: tuple) -> dict[str, Any]:
    d = dict(zip(cols, row))
    for k in _INT_COLS:
        d[k] = int(d.get(k) or 0)
    d["TotalMB"] = float(d.get("TotalMB") or 0)
    d["IsHeap"] = bool(d["IsHeap"])
    d["HasPK"] = bool(d["HasPK"])
    # cold = no read/write activity recorded since the last SQL Server restart
    d["ColdSinceRestart"] = (d["Writes"] == 0 and d["Reads"] == 0)
    d["ReportCount"] = 0        # filled in by the SSRS pass if enabled
    d["ReportSamples"] = []
    return d


# ─────────────────────────────────────────────────────────────────────────────
# SSRS report-usage (optional) — heuristic table-name match in report queries
# ─────────────────────────────────────────────────────────────────────────────

# Report definitions (RDL) live in ReportServer.dbo.Catalog as XML in Content.
_SSRS_DB = "ReportServer"
_REPORTS_SQL = f"""
SELECT c.Path,
       CONVERT(varchar(max), CONVERT(varbinary(max), c.Content)) AS Rdl
FROM {_SSRS_DB}.dbo.[Catalog] c
WHERE c.Type = 2 AND c.Content IS NOT NULL
"""

# Pull the SQL text out of each dataset's <CommandText>…</CommandText>. If a
# report has none (shared datasets, embedded expressions), fall back to the whole
# RDL so we don't miss references.
_COMMANDTEXT_RE = re.compile(r"<CommandText>(.*?)</CommandText>", re.IGNORECASE | re.DOTALL)
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
_SAMPLE_CAP = 25   # max report paths kept per table for the drill-down


def _report_tokens(rdl: str) -> set[str]:
    """Lowercased identifier tokens from a report's query text (or whole RDL)."""
    parts = _COMMANDTEXT_RE.findall(rdl)
    text = " ".join(parts) if parts else rdl
    return {m.lower() for m in _TOKEN_RE.findall(text)}


def _match_reports(reports: list[tuple], table_names: list[str]) -> dict[str, dict]:
    """
    For each table name, count/sample reports whose query tokens contain it.
    Word-token intersection avoids substring false-positives (e.g. 'Marks' would
    not match 'Remarks'). Returns {table_name_lower: {count, samples}}.
    """
    name_set = {n.lower() for n in table_names}
    out: dict[str, dict] = {n: {"count": 0, "samples": []} for n in name_set}
    for path, rdl in reports:
        if not rdl:
            continue
        for hit in (_report_tokens(rdl) & name_set):
            rec = out[hit]
            rec["count"] += 1
            if len(rec["samples"]) < _SAMPLE_CAP:
                rec["samples"].append(path)
    return out


def _apply_ssrs(conn: pyodbc.Connection, tables: list[dict]) -> tuple[bool, int, Optional[str]]:
    """
    Best-effort SSRS enrichment. Mutates `tables` in place (ReportCount/Samples).
    Returns (available, total_reports_scanned, note). Never raises — SSRS being
    absent or unreadable must not fail the whole feature.
    """
    try:
        cur = conn.cursor()
        cur.execute(_REPORTS_SQL)
        reports = [(r[0], r[1]) for r in cur.fetchall()]
    except pyodbc.Error as exc:
        logger.info("table_intelligence: SSRS scan skipped (%s)", exc.args[0] if exc.args else exc)
        return False, 0, ("ReportServer database not found or not readable — "
                          "SSRS report usage was skipped.")

    matches = _match_reports(reports, [t["TableName"] for t in tables])
    for t in tables:
        rec = matches.get(t["TableName"].lower())
        if rec:
            t["ReportCount"] = rec["count"]
            t["ReportSamples"] = rec["samples"]
    return True, len(reports), None


# ─────────────────────────────────────────────────────────────────────────────
# Result envelope + public entry point
# ─────────────────────────────────────────────────────────────────────────────

def _result(*, status, tables=None, server_start_time=None, ssrs_available=False,
            ssrs_report_count=0, ssrs_note=None, error=None, error_kind=None, message="") -> dict:
    tables = tables or []
    return {
        "status": status,
        "total_tables": len(tables),
        "server_start_time": server_start_time,
        "ssrs_available": ssrs_available,
        "ssrs_report_count": ssrs_report_count,
        "ssrs_note": ssrs_note,
        "tables": tables,
        "error": error,
        "error_kind": error_kind,
        "message": message,
    }


def run_table_intelligence(
    conn: pyodbc.Connection,
    include_ssrs: bool = True,
) -> dict[str, Any]:
    """
    Build a per-table profile for every user table. Read-only.

    Steps: 1) server start time (activity-stats validity window) →
           2) one native metrics query → 3) optional SSRS enrichment →
           4) combined result. SSRS failure degrades gracefully (feature still
           returns, with ssrs_available=False).
    """
    # ── server start time (context for the reset-on-restart activity stats) ──
    server_start = None
    try:
        cur = conn.cursor()
        cur.execute("SELECT CONVERT(varchar(19), sqlserver_start_time, 120) FROM sys.dm_os_sys_info")
        row = cur.fetchone()
        server_start = row[0] if row else None
    except pyodbc.Error:
        logger.warning("table_intelligence: could not read server start time", exc_info=True)

    # ── native per-table metrics ─────────────────────────────────────────────
    try:
        cur = conn.cursor()
        cur.execute(_METRICS_SQL)
        cols = [d[0] for d in cur.description]
        tables = [_row_to_dict(cols, r) for r in cur.fetchall()]
    except pyodbc.Error:
        logger.error("table_intelligence: metrics query failed", exc_info=True)
        return _result(status="error", error_kind="db_error",
                       error="Failed to query table metadata.",
                       message="Database query failed.")

    if not tables:
        return _result(status="empty", server_start_time=server_start,
                       message="No user tables found in this database.")

    # ── optional SSRS enrichment ─────────────────────────────────────────────
    ssrs_available, ssrs_count, ssrs_note = False, 0, None
    if include_ssrs:
        ssrs_available, ssrs_count, ssrs_note = _apply_ssrs(conn, tables)

    return _result(status="ok", tables=tables, server_start_time=server_start,
                   ssrs_available=ssrs_available, ssrs_report_count=ssrs_count,
                   ssrs_note=ssrs_note, message="ok")
