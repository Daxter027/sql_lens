"""
ghost_pages.py
--------------
Analysis module for Issue 5: Force Reconciliation of Ghost Page Data.

Phase 1: analyze() is fully implemented with real DMV T-SQL.
         execute() is stubbed — ghost page remediation is NOT in scope for Phase 1.

TODO (Phase 2): Implement execute() to:
  - Run ALTER INDEX ... REORGANIZE (for fragmentation < 30%) to trigger
    ghost record cleanup without exclusive locks
  - Run ALTER INDEX ... REBUILD (for fragmentation >= 30%) with configurable
    ONLINE option based on edition
  - NEVER use DBCC page-level commands (e.g. DBCC PAGE, DBCC WRITEPAGE) —
    these are explicitly out of scope for this tool in ALL versions due to
    risk of data corruption. Only standard index maintenance operations.

GHOST PAGE BACKGROUND:
  When rows are deleted, SQL Server doesn't immediately remove them from the
  page. A background ghost cleanup task handles this, but under heavy load or
  specific conditions it can lag. Ghost records waste buffer pool space and
  slow scans. Index REORGANIZE/REBUILD forces immediate cleanup.
"""

from __future__ import annotations
import logging
from typing import Any
import pyodbc
from config import GHOST_RECORD_MIN_COUNT, GHOST_MIN_PAGES

logger = logging.getLogger(__name__)

ISSUE_ID   = "ghost_pages"
ISSUE_NAME = "Ghost Page Data Reconciliation"


def analyze(conn: pyodbc.Connection) -> dict[str, Any]:
    """
    Find tables/indexes with significant ghost record counts using
    sys.dm_db_index_physical_stats. Also captures fragmentation % to
    inform the REORGANIZE vs REBUILD recommendation.

    ghost_record_count lives on the LEAF pages. LIMITED mode does not read leaf
    pages (it returns NULL for ghosts) so it can never detect them; SAMPLED does.
    But running SAMPLED over the WHOLE database enumerates and samples every index
    of every table — minutes on a large server. So we first cheaply pick the
    sizable tables (>= GHOST_MIN_PAGES) from metadata, then run a SAMPLED scan
    scoped to each of those tables only. Small tables are skipped (noted below).
    """
    cursor = conn.cursor()

    # ── Step 1: cheap metadata pass — pick tables big enough to matter ────────
    # sys.dm_db_partition_stats is metadata only (no page reads), so this is fast
    # even on a huge database. We sum pages across all of a table's partitions.
    cursor.execute("""
        SELECT ps.object_id, SUM(ps.used_page_count) AS pages
        FROM sys.dm_db_partition_stats ps
        JOIN sys.tables t ON t.object_id = ps.object_id
        WHERE t.is_ms_shipped = 0
        GROUP BY ps.object_id
        HAVING SUM(ps.used_page_count) >= ?
        ORDER BY SUM(ps.used_page_count) DESC
    """, GHOST_MIN_PAGES)
    candidate_object_ids = [row[0] for row in cursor.fetchall()]

    # ── Step 2: SAMPLED physical scan, scoped to each sizable table ───────────
    findings = []
    for object_id in candidate_object_ids:
        try:
            cursor.execute("""
                SELECT
                    s.name                          AS schema_name,
                    t.name                          AS table_name,
                    i.name                          AS index_name,
                    ips.index_type_desc,
                    ips.ghost_record_count,
                    ips.avg_fragmentation_in_percent,
                    ips.page_count,
                    ips.record_count
                FROM sys.dm_db_index_physical_stats(DB_ID(), ?, NULL, NULL, 'SAMPLED') ips
                JOIN sys.tables  t ON t.object_id = ips.object_id
                JOIN sys.schemas s ON s.schema_id  = t.schema_id
                LEFT JOIN sys.indexes i ON i.object_id = ips.object_id
                                        AND i.index_id = ips.index_id
                WHERE ips.ghost_record_count >= ?
            """, object_id, GHOST_RECORD_MIN_COUNT)
            rows = cursor.fetchall()
        except pyodbc.Error:
            continue  # a single object failing should not abort the whole check

        for row in rows:
            (schema_name, table_name, index_name, index_type_desc,
             ghost_count, frag_pct, page_count, record_count) = row

            frag = round(float(frag_pct), 1) if frag_pct else 0
            action = (
                "ALTER INDEX REBUILD" if frag >= 30
                else "ALTER INDEX REORGANIZE"
            )

            findings.append({
                "schema":             schema_name,
                "table":              table_name,
                "index":              index_name or "(heap)",
                "index_type":         index_type_desc,
                "ghost_record_count": ghost_count,
                "fragmentation_pct":  frag,
                "page_count":         page_count,
                "record_count":       record_count,
                "recommended_op":     action,
            })

    findings.sort(key=lambda f: f["ghost_record_count"], reverse=True)

    scan_note = (
        f"SAMPLED scan of {len(candidate_object_ids)} table(s) >= {GHOST_MIN_PAGES:,} pages "
        f"(~{GHOST_MIN_PAGES * 8 // 1024} MB); smaller tables skipped for performance."
    )

    if not findings:
        return {
            "issue_id":         ISSUE_ID,
            "issue_name":       ISSUE_NAME,
            "severity":         "Low",
            "affected_objects": [],
            "current_metrics":  {"affected_indexes": 0, "total_ghost_records": 0},
            "recommended_action": "No significant ghost record accumulation found.",
            "estimated_impact": "N/A",
            "executable":       False,
            "eligible_for_fix": False,
            "blocking_reason":  "No tables with significant ghost records found.",
            "analysis_note":    scan_note,
        }

    total_ghosts = sum(f["ghost_record_count"] for f in findings)
    severity = "High" if total_ghosts > 1_000_000 else "Medium" if total_ghosts > 100_000 else "Low"

    return {
        "issue_id":         ISSUE_ID,
        "issue_name":       ISSUE_NAME,
        "severity":         severity,
        "affected_objects": findings,
        "current_metrics": {
            "affected_indexes":    len(findings),
            "total_ghost_records": total_ghosts,
        },
        "recommended_action": (
            f"Found {len(findings)} index(es) with {total_ghosts:,} total ghost records. "
            "The fix runs ALTER INDEX REORGANIZE (fragmentation < 30%) or REBUILD "
            "(fragmentation >= 30%) per object to force ghost cleanup — REBUILD attempts "
            "ONLINE=ON first and falls back to OFFLINE on Standard/Express. Heaps are "
            "rebuilt with ALTER TABLE ... REBUILD. "
            "DBCC page-level commands are never used (corruption risk). "
            "Run REBUILDs during a low-traffic window."
        ),
        "estimated_impact": (
            f"Cleanup of {total_ghosts:,} ghost records; "
            "reduced scan overhead and buffer pool waste."
        ),
        "executable":       True,
        "eligible_for_fix": True,
        "blocking_reason":  None,
        "analysis_note":    scan_note,
    }


def _ghost_count(cursor, object_id: int, index_id: int):
    """Sum ghost_record_count for one object/index via a SAMPLED physical scan.
    SAMPLED (not LIMITED) is required — LIMITED returns NULL for ghost counts."""
    try:
        cursor.execute(
            "SELECT ISNULL(SUM(ghost_record_count), 0) "
            "FROM sys.dm_db_index_physical_stats(DB_ID(), ?, ?, NULL, 'SAMPLED')",
            object_id, index_id,
        )
        return int(cursor.fetchone()[0])
    except pyodbc.Error:
        return None


def _process_single(
    conn: pyodbc.Connection,
    schema_name: str,
    table_name: str,
    index_name: str | None,
    frag_pct: float,
) -> dict:
    """REORGANIZE or REBUILD a single object to force ghost-record cleanup."""
    cursor = conn.cursor()
    is_heap = index_name is None or index_name == "(heap)"
    base = {
        "schema": schema_name, "table": table_name,
        "index": index_name or "(heap)",
        "command_executed": None, "before_metrics": None, "after_metrics": None,
    }

    # ── Resolve object_id / index_id and confirm it still exists ─────────────
    if is_heap:
        cursor.execute("""
            SELECT t.object_id, i.index_id
            FROM sys.tables t
            JOIN sys.schemas s ON s.schema_id = t.schema_id
            JOIN sys.indexes i ON i.object_id = t.object_id AND i.index_id = 0
            WHERE s.name = ? AND t.name = ?
        """, schema_name, table_name)
    else:
        cursor.execute("""
            SELECT t.object_id, i.index_id
            FROM sys.tables t
            JOIN sys.schemas s ON s.schema_id = t.schema_id
            JOIN sys.indexes i ON i.object_id = t.object_id AND i.name = ?
            WHERE s.name = ? AND t.name = ?
        """, index_name, schema_name, table_name)
    row = cursor.fetchone()
    if not row:
        return {**base, "status": "skipped",
                "message": f"Object no longer exists on [{schema_name}].[{table_name}]."}
    object_id, index_id = row

    # ── Permission check ─────────────────────────────────────────────────────
    try:
        cursor.execute(
            "SELECT HAS_PERMS_BY_NAME(?, 'OBJECT', 'ALTER'), IS_SRVROLEMEMBER('sysadmin')",
            f"{schema_name}.{table_name}",
        )
        has_alter, is_sysadmin = cursor.fetchone()
        if not has_alter and not is_sysadmin:
            return {**base, "status": "skipped",
                    "message": "Current login lacks ALTER permission on the table."}
    except pyodbc.Error:
        pass

    before_ghosts = _ghost_count(cursor, object_id, index_id)

    # ── Decide operation ─────────────────────────────────────────────────────
    # Heaps cannot be REORGANIZE'd — always rebuilt via ALTER TABLE.
    use_rebuild = is_heap or frag_pct >= 30

    def _run(online: bool | None):
        if not use_rebuild:
            # REORGANIZE is always online and takes no ONLINE option.
            sql = ("DECLARE @sql NVARCHAR(MAX) = N'ALTER INDEX ' + QUOTENAME(?) + "
                   "N' ON ' + QUOTENAME(?) + N'.' + QUOTENAME(?) + N' REORGANIZE'; "
                   "EXEC sp_executesql @sql;")
            cursor.execute(sql, index_name, schema_name, table_name)
            return
        opt = "ON" if online else "OFF"
        if is_heap:
            sql = ("DECLARE @sql NVARCHAR(MAX) = N'ALTER TABLE ' + QUOTENAME(?) + N'.' + "
                   "QUOTENAME(?) + N' REBUILD WITH (ONLINE = " + opt + ")'; "
                   "EXEC sp_executesql @sql;")
            cursor.execute(sql, schema_name, table_name)
        else:
            sql = ("DECLARE @sql NVARCHAR(MAX) = N'ALTER INDEX ' + QUOTENAME(?) + "
                   "N' ON ' + QUOTENAME(?) + N'.' + QUOTENAME(?) + "
                   "N' REBUILD WITH (ONLINE = " + opt + ")'; EXEC sp_executesql @sql;")
            cursor.execute(sql, index_name, schema_name, table_name)

    target = (f"[{schema_name}].[{table_name}]" if is_heap
              else f"[{index_name}] ON [{schema_name}].[{table_name}]")
    online_used = False
    conn.autocommit = True
    try:
        if use_rebuild:
            try:
                _run(online=True)
                online_used = True
            except pyodbc.Error as exc:
                if "1844" in str(exc) or "online" in str(exc).lower():
                    logger.warning("ONLINE=ON unsupported — retrying REBUILD OFFLINE on %s", target)
                    _run(online=False)
                else:
                    raise
            op_label = "REBUILD" + (" (ONLINE=ON)" if online_used else " (ONLINE=OFF)")
            audit_cmd = (f"ALTER {'TABLE' if is_heap else 'INDEX'} {target} REBUILD "
                         f"WITH (ONLINE = {'ON' if online_used else 'OFF'})")
        else:
            _run(online=None)
            op_label = "REORGANIZE"
            audit_cmd = f"ALTER INDEX {target} REORGANIZE"
        logger.info("Ghost cleanup %s on %s — success.", op_label, target)
    except pyodbc.Error:
        conn.autocommit = False
        logger.error("Ghost cleanup failed (details not forwarded to client)", exc_info=True)
        return {**base, "status": "failed",
                "message": f"Failed to run index maintenance on {target}.",
                "before_metrics": {"ghost_record_count": before_ghosts}}
    finally:
        conn.autocommit = False

    after_ghosts = _ghost_count(cursor, object_id, index_id)
    return {
        **base, "status": "success", "command_executed": audit_cmd,
        "message": (f"{op_label} completed. Ghost records: "
                    f"{before_ghosts if before_ghosts is not None else '?'} → "
                    f"{after_ghosts if after_ghosts is not None else '?'}."),
        "before_metrics": {"ghost_record_count": before_ghosts},
        "after_metrics":  {"ghost_record_count": after_ghosts},
    }


def execute(
    conn: pyodbc.Connection,
    target_schema: str | None = None,
    target_table:  str | None = None,
    target_column: str | None = None,  # reused to carry the index name when targeting one
) -> dict:
    """
    Force ghost-record cleanup via targeted ALTER INDEX REORGANIZE/REBUILD.

    With no explicit target, every flagged object from analyze() is processed.
    Page-level DBCC commands are never used.
    """
    targets = []
    if target_schema and target_table:
        targets.append((target_schema, target_table, target_column, 0.0))
    else:
        analysis = analyze(conn)
        for f in analysis.get("affected_objects", []):
            targets.append((f["schema"], f["table"], f["index"], f.get("fragmentation_pct", 0)))
        if not targets:
            return {"status": "skipped",
                    "message": "No objects with significant ghost records were found.",
                    "results": []}

    results = []
    success = fail = 0
    for sch, tbl, idx, frag in targets:
        res = _process_single(conn, sch, tbl, idx, frag)
        results.append(res)
        if res["status"] == "success":
            success += 1
        elif res["status"] == "failed":
            fail += 1

    status = "success"
    if fail:
        status = "partial" if success else "failed"

    if len(targets) == 1:
        message = results[0]["message"]
    else:
        message = f"Processed {len(targets)} object(s): {success} cleaned, {fail} failed."

    return {"status": status, "message": message, "results": results}
