"""
index_fragmentation.py
----------------------
Analysis + execution module: Find and rebuild fragmented rowstore indexes.

analyze() scans index physical stats (LIMITED mode — cheap, no leaf reads) and
flags rowstore indexes whose fragmentation is at/above the REORG threshold and
that are large enough to matter (>= INDEX_FRAG_MIN_PAGES). Each candidate is
tagged REORGANIZE (10–30%) or REBUILD (>= 30%) per Microsoft guidance.

execute() runs ALTER INDEX ... REORGANIZE / REBUILD per object. REBUILD attempts
ONLINE = ON first and falls back to ONLINE = OFF on Standard/Express. It NEVER
uses DBCC page-level commands.

SCOPE: rowstore non-heap indexes only (index_id >= 1). Heaps are deliberately
excluded — defragmenting a heap is the job of the heap-clustering check, and
"rebuild fragmented indexes" should not silently rewrite heaps. This keeps the
two features from stepping on each other.
"""

from __future__ import annotations
import logging
from typing import Any, Optional
import pyodbc
from config import (
    INDEX_FRAG_REORG_THRESHOLD,
    INDEX_FRAG_REBUILD_THRESHOLD,
    INDEX_FRAG_MIN_PAGES,
)

logger = logging.getLogger(__name__)

ISSUE_ID   = "index_fragmentation"
ISSUE_NAME = "Fragmented Index Rebuild"


# ─────────────────────────────────────────────────────────────────────────────
# Pure decision helpers (unit-tested without a DB)
# ─────────────────────────────────────────────────────────────────────────────

def recommend_op(frag_pct: float) -> str:
    """REBUILD at/above the rebuild threshold, else REORGANIZE."""
    return "REBUILD" if frag_pct >= INDEX_FRAG_REBUILD_THRESHOLD else "REORGANIZE"


def is_candidate(frag_pct: Optional[float], page_count: Optional[int]) -> bool:
    """An index is worth maintaining only if both big enough and fragmented enough."""
    if frag_pct is None or page_count is None:
        return False
    return frag_pct >= INDEX_FRAG_REORG_THRESHOLD and page_count >= INDEX_FRAG_MIN_PAGES


def severity_for(rebuild_count: int, max_frag: float) -> str:
    if rebuild_count > 10 or max_frag >= 50:
        return "High"
    if rebuild_count > 0 or max_frag >= INDEX_FRAG_REORG_THRESHOLD:
        return "Medium"
    return "Low"


# ─────────────────────────────────────────────────────────────────────────────
# Analyze
# ─────────────────────────────────────────────────────────────────────────────

def analyze(conn: pyodbc.Connection) -> dict[str, Any]:
    """
    Find fragmented rowstore indexes. Like ghost_pages, we first cheaply pick
    sizable tables from metadata, then run a LIMITED physical-stats scan scoped to
    each — far cheaper than one whole-database physical scan on a large server.
    """
    cursor = conn.cursor()

    # ── Step 1: cheap metadata pass — only tables big enough to matter ────────
    cursor.execute("""
        SELECT ps.object_id, SUM(ps.used_page_count) AS pages
        FROM sys.dm_db_partition_stats ps
        JOIN sys.tables t ON t.object_id = ps.object_id
        WHERE t.is_ms_shipped = 0
        GROUP BY ps.object_id
        HAVING SUM(ps.used_page_count) >= ?
        ORDER BY SUM(ps.used_page_count) DESC
    """, INDEX_FRAG_MIN_PAGES)
    candidate_object_ids = [row[0] for row in cursor.fetchall()]

    # ── Step 2: LIMITED physical scan per sizable table ───────────────────────
    findings: list[dict] = []
    for object_id in candidate_object_ids:
        try:
            cursor.execute("""
                SELECT
                    s.name                          AS schema_name,
                    t.name                          AS table_name,
                    i.name                          AS index_name,
                    ips.index_type_desc,
                    ips.avg_fragmentation_in_percent,
                    ips.page_count
                FROM sys.dm_db_index_physical_stats(DB_ID(), ?, NULL, NULL, 'LIMITED') ips
                JOIN sys.tables  t ON t.object_id = ips.object_id
                JOIN sys.schemas s ON s.schema_id  = t.schema_id
                JOIN sys.indexes i ON i.object_id  = ips.object_id
                                   AND i.index_id  = ips.index_id
                WHERE ips.index_id >= 1                 -- rowstore non-heap only
                  AND i.is_disabled = 0
                  AND ips.index_type_desc IN ('CLUSTERED INDEX', 'NONCLUSTERED INDEX')
                  AND ips.page_count >= ?
                  AND ips.avg_fragmentation_in_percent >= ?
            """, object_id, INDEX_FRAG_MIN_PAGES, float(INDEX_FRAG_REORG_THRESHOLD))
            rows = cursor.fetchall()
        except pyodbc.Error:
            continue  # one object failing must not abort the whole check

        for row in rows:
            (schema_name, table_name, index_name, index_type, frag_pct, page_count) = row
            frag = round(float(frag_pct), 1) if frag_pct is not None else 0.0
            findings.append({
                "schema":            schema_name,
                "table":             table_name,
                "index":             index_name,
                "index_type":        index_type,
                "fragmentation_pct": frag,
                "page_count":        int(page_count),
                "size_mb":           round(int(page_count) * 8 / 1024, 2),
                "recommended_op":    recommend_op(frag),
            })

    findings.sort(key=lambda f: f["fragmentation_pct"], reverse=True)

    scan_note = (
        f"LIMITED scan of {len(candidate_object_ids)} table(s) >= "
        f"{INDEX_FRAG_MIN_PAGES:,} pages (~{INDEX_FRAG_MIN_PAGES * 8 // 1024} MB); "
        f"flagged indexes >= {INDEX_FRAG_REORG_THRESHOLD:.0f}% fragmented. "
        "Heaps are out of scope (see heap-clustering check)."
    )

    if not findings:
        return {
            "issue_id":         ISSUE_ID,
            "issue_name":       ISSUE_NAME,
            "severity":         "Low",
            "affected_objects": [],
            "current_metrics":  {"fragmented_indexes": 0, "reorganize_count": 0,
                                 "rebuild_count": 0, "max_fragmentation_pct": 0,
                                 "total_size_mb": 0},
            "recommended_action": "No significantly fragmented indexes found.",
            "estimated_impact": "N/A",
            "executable":       False,
            "eligible_for_fix": False,
            "blocking_reason":  "No indexes meet the fragmentation/size thresholds.",
            "analysis_note":    scan_note,
        }

    rebuild_count   = sum(1 for f in findings if f["recommended_op"] == "REBUILD")
    reorg_count     = len(findings) - rebuild_count
    max_frag        = max(f["fragmentation_pct"] for f in findings)
    total_size_mb   = round(sum(f["size_mb"] for f in findings), 2)
    severity        = severity_for(rebuild_count, max_frag)

    return {
        "issue_id":         ISSUE_ID,
        "issue_name":       ISSUE_NAME,
        "severity":         severity,
        "affected_objects": findings,
        "current_metrics": {
            "fragmented_indexes":    len(findings),
            "reorganize_count":      reorg_count,
            "rebuild_count":         rebuild_count,
            "max_fragmentation_pct": max_frag,
            "total_size_mb":         total_size_mb,
        },
        "recommended_action": (
            f"Found {len(findings)} fragmented index(es) "
            f"({reorg_count} to REORGANIZE, {rebuild_count} to REBUILD; "
            f"worst {max_frag:.0f}%). The fix runs ALTER INDEX REORGANIZE "
            f"(< {INDEX_FRAG_REBUILD_THRESHOLD:.0f}%) or REBUILD "
            f"(>= {INDEX_FRAG_REBUILD_THRESHOLD:.0f}%) per index — REBUILD attempts "
            "ONLINE=ON first and falls back to OFFLINE on Standard/Express. "
            "Run REBUILDs during a low-traffic window; they fully rewrite the index "
            "and generate transaction-log activity."
        ),
        "estimated_impact": (
            f"Reduced fragmentation across {total_size_mb:,.0f} MB of indexes; "
            "better range-scan I/O and read-ahead efficiency."
        ),
        "executable":       True,
        "eligible_for_fix": True,
        "blocking_reason":  None,
        "analysis_note":    scan_note,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Execute
# ─────────────────────────────────────────────────────────────────────────────

def _frag_pct(cursor, object_id: int, index_id: int) -> Optional[float]:
    """Current avg fragmentation for one index via a LIMITED physical scan."""
    try:
        cursor.execute(
            "SELECT MAX(avg_fragmentation_in_percent) "
            "FROM sys.dm_db_index_physical_stats(DB_ID(), ?, ?, NULL, 'LIMITED')",
            object_id, index_id,
        )
        v = cursor.fetchone()[0]
        return round(float(v), 1) if v is not None else None
    except pyodbc.Error:
        return None


def _process_single(
    conn: pyodbc.Connection,
    schema_name: str,
    table_name: str,
    index_name: str,
    frag_pct: float,
) -> dict:
    """REORGANIZE or REBUILD a single rowstore index based on fragmentation."""
    cursor = conn.cursor()
    base = {
        "schema": schema_name, "table": table_name, "index": index_name,
        "command_executed": None, "before_metrics": None, "after_metrics": None,
    }

    # ── Resolve object_id/index_id and confirm it still exists & is enabled ───
    cursor.execute("""
        SELECT t.object_id, i.index_id, i.is_disabled
        FROM sys.tables t
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        JOIN sys.indexes i ON i.object_id = t.object_id AND i.name = ?
        WHERE s.name = ? AND t.name = ? AND i.index_id >= 1
    """, index_name, schema_name, table_name)
    row = cursor.fetchone()
    if not row:
        return {**base, "status": "skipped",
                "message": f"Index [{index_name}] no longer exists on [{schema_name}].[{table_name}]."}
    object_id, index_id, is_disabled = row
    if is_disabled:
        return {**base, "status": "skipped",
                "message": f"Index [{index_name}] is disabled — rebuild it via the unused-index flow if intended."}

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

    before_frag = _frag_pct(cursor, object_id, index_id)
    use_rebuild = frag_pct >= INDEX_FRAG_REBUILD_THRESHOLD

    def _run(online: Optional[bool]):
        if not use_rebuild:
            sql = ("DECLARE @sql NVARCHAR(MAX) = N'ALTER INDEX ' + QUOTENAME(?) + "
                   "N' ON ' + QUOTENAME(?) + N'.' + QUOTENAME(?) + N' REORGANIZE'; "
                   "EXEC sp_executesql @sql;")
            cursor.execute(sql, index_name, schema_name, table_name)
            return
        opt = "ON" if online else "OFF"
        sql = ("DECLARE @sql NVARCHAR(MAX) = N'ALTER INDEX ' + QUOTENAME(?) + "
               "N' ON ' + QUOTENAME(?) + N'.' + QUOTENAME(?) + "
               "N' REBUILD WITH (ONLINE = " + opt + ")'; EXEC sp_executesql @sql;")
        cursor.execute(sql, index_name, schema_name, table_name)

    target = f"[{index_name}] ON [{schema_name}].[{table_name}]"
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
            audit_cmd = f"ALTER INDEX {target} REBUILD WITH (ONLINE = {'ON' if online_used else 'OFF'})"
        else:
            _run(online=None)
            op_label = "REORGANIZE"
            audit_cmd = f"ALTER INDEX {target} REORGANIZE"
        logger.info("Index maintenance %s on %s — success.", op_label, target)
    except pyodbc.Error:
        conn.autocommit = False
        logger.error("Index maintenance failed (details not forwarded to client)", exc_info=True)
        return {**base, "status": "failed",
                "message": f"Failed to run index maintenance on {target}.",
                "before_metrics": {"fragmentation_pct": before_frag}}
    finally:
        conn.autocommit = False

    after_frag = _frag_pct(cursor, object_id, index_id)
    return {
        **base, "status": "success", "command_executed": audit_cmd,
        "message": (f"{op_label} completed. Fragmentation: "
                    f"{before_frag if before_frag is not None else '?'}% → "
                    f"{after_frag if after_frag is not None else '?'}%."),
        "before_metrics": {"fragmentation_pct": before_frag},
        "after_metrics":  {"fragmentation_pct": after_frag},
    }


def execute(
    conn: pyodbc.Connection,
    target_schema: str | None = None,
    target_table:  str | None = None,
    target_column: str | None = None,   # reused to carry the index name when targeting one
) -> dict:
    """
    REORGANIZE/REBUILD fragmented indexes. With no explicit target, every
    candidate from analyze() is processed. Page-level DBCC commands are never used.
    """
    targets = []
    if target_schema and target_table and target_column:
        targets.append((target_schema, target_table, target_column, 100.0))  # explicit target → force REBUILD path
    else:
        analysis = analyze(conn)
        for f in analysis.get("affected_objects", []):
            targets.append((f["schema"], f["table"], f["index"], f.get("fragmentation_pct", 0)))
        if not targets:
            return {"status": "skipped",
                    "message": "No fragmented indexes met the thresholds.",
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
        message = f"Processed {len(targets)} index(es): {success} maintained, {fail} failed."

    return {"status": status, "message": message, "results": results}
