"""
heap_clustering.py
------------------
Analysis and execution module for Issue 2: Clustered Index Conversion for Unordered Heaps.

Phase 2 complete:
  analyze() — identifies heap tables with candidate clustering keys using DMV T-SQL.
  execute() — creates a clustered index on the target table.
              Attempts ONLINE = ON first (Enterprise/Developer edition);
              falls back to ONLINE = OFF (Standard/Express) with a clear warning.
              Post-verifies via sys.indexes that type_desc flipped from HEAP to CLUSTERED.

PRODUCTION NOTE: CREATE CLUSTERED INDEX physically rewrites the entire table and
all non-clustered indexes. Even ONLINE = ON holds brief Sch-M locks at start/end.
Run during a low-traffic window and ensure adequate free disk space (≈1× table size).
"""

from __future__ import annotations
import logging
from typing import Any
import pyodbc
from config import HEAP_MIN_ROW_COUNT

logger = logging.getLogger(__name__)

ISSUE_ID   = "heap_clustering"
ISSUE_NAME = "Clustered Index Conversion for Unordered Heaps"


def analyze(conn: pyodbc.Connection) -> dict[str, Any]:
    """
    Identify heap tables (no clustered index) above the minimum row threshold.
    For each heap, identify the best clustering key candidate:
      1. Existing primary key column(s)
      2. Existing unique constraint column
      3. Identity column
      4. None found — noted explicitly
    """
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            t.name                          AS table_name,
            s.name                          AS schema_name,
            p.rows                          AS row_count,
            CAST(
                SUM(a.total_pages) * 8.0 / 1024
            AS DECIMAL(18,2))               AS size_mb,
            -- Best candidate key: PK > unique constraint > identity > none
            ISNULL(
                (SELECT TOP 1 COL_NAME(ic.object_id, ic.column_id)
                 FROM sys.index_columns ic
                 JOIN sys.indexes ix ON ix.object_id = ic.object_id
                                     AND ix.index_id = ic.index_id
                 WHERE ix.object_id = t.object_id
                   AND ix.is_primary_key = 1
                 ORDER BY ic.key_ordinal),
                ISNULL(
                    (SELECT TOP 1 COL_NAME(ic.object_id, ic.column_id)
                     FROM sys.index_columns ic
                     JOIN sys.indexes ix ON ix.object_id = ic.object_id
                                         AND ix.index_id = ic.index_id
                     WHERE ix.object_id = t.object_id
                       AND ix.is_unique_constraint = 1
                     ORDER BY ic.key_ordinal),
                    (SELECT TOP 1 c.name
                     FROM sys.columns c
                     WHERE c.object_id = t.object_id
                       AND c.is_identity = 1)
                )
            )                               AS candidate_key
        FROM sys.tables t
        JOIN sys.schemas s       ON s.schema_id = t.schema_id
        JOIN sys.indexes i       ON i.object_id = t.object_id
                                AND i.index_id = 0          -- 0 = heap
        JOIN sys.partitions p    ON p.object_id = t.object_id
                                AND p.index_id = 0
        JOIN sys.allocation_units a ON a.container_id = p.hobt_id
        WHERE t.is_ms_shipped = 0
          AND p.rows >= ?
        GROUP BY t.name, s.name, t.object_id, p.rows
        ORDER BY p.rows DESC
    """, HEAP_MIN_ROW_COUNT)

    heaps = []
    for row in cursor.fetchall():
        table_name, schema_name, row_count, size_mb, candidate_key = row
        heaps.append({
            "schema":        schema_name,
            "table":         table_name,
            "row_count":     row_count,
            "size_mb":       float(size_mb) if size_mb else 0,
            "candidate_key": candidate_key or "None found — manual analysis required",
        })

    if not heaps:
        return {
            "issue_id":         ISSUE_ID,
            "issue_name":       ISSUE_NAME,
            "severity":         "Low",
            "affected_objects": [],
            "current_metrics":  {"heap_count": 0},
            "recommended_action": "No large heap tables found above the threshold.",
            "estimated_impact": "N/A",
            "executable":       False,
            "eligible_for_fix": False,
            "blocking_reason":  "No heap tables meet the minimum row threshold.",
            "analysis_note":    f"Checked tables with >= {HEAP_MIN_ROW_COUNT:,} rows.",
        }

    total_size = round(sum(h["size_mb"] for h in heaps), 2)
    severity = "High" if len(heaps) > 10 else "Medium" if len(heaps) > 3 else "Low"

    # Heaps with a resolved candidate key are eligible for automated execution.
    actionable = [h for h in heaps if "manual analysis" not in h["candidate_key"]]

    return {
        "issue_id":         ISSUE_ID,
        "issue_name":       ISSUE_NAME,
        "severity":         severity,
        "affected_objects": heaps,
        "current_metrics":  {
            "heap_count":       len(heaps),
            "total_size_mb":    total_size,
            "actionable_count": len(actionable),
        },
        "recommended_action": (
            f"Found {len(heaps)} heap table(s) totalling {total_size:.0f} MB. "
            f"{len(actionable)} have a resolvable candidate key and can be fixed automatically. "
            "Adding a clustered index improves range-scan performance and reduces "
            "fragmentation. ONLINE=ON is attempted first and falls back to OFFLINE on "
            "Standard/Express editions. Prefer identity or PK columns as the clustering key "
            "to minimise page splits. Run during a low-traffic window for OFFLINE rebuilds."
        ),
        "estimated_impact": (
            f"Improved read performance on {len(heaps)} table(s); "
            "reduced forwarded-record overhead."
        ),
        "executable":       True,
        "eligible_for_fix": len(actionable) > 0,
        "blocking_reason":  (
            None if actionable else
            "All detected heaps require manual key selection — no identity/PK/unique column found."
        ),
        "analysis_note":    f"Filtered to tables with >= {HEAP_MIN_ROW_COUNT:,} rows.",
    }


def _process_single_heap(
    conn: pyodbc.Connection,
    target_schema: str,
    target_table: str,
    target_column: str,
) -> dict:
    """Helper to process a single heap and return an individual result dict."""
    cursor = conn.cursor()

    # ── Pre-check 1: Confirm the table is still a heap ────────────────────────
    cursor.execute("""
        SELECT i.type_desc, p.rows
        FROM sys.tables t
        JOIN sys.schemas s  ON s.schema_id = t.schema_id
        JOIN sys.indexes i  ON i.object_id  = t.object_id AND i.index_id = 0
        JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id = 0
        WHERE s.name = ? AND t.name = ?
    """, target_schema, target_table)
    row = cursor.fetchone()
    if not row:
        return {
            "target_schema": target_schema, "target_table": target_table, "target_column": target_column,
            "status":  "skipped",
            "message": f"[{target_schema}].[{target_table}] not found or is no longer a heap.",
            "command_executed": None, "before_metrics": None, "after_metrics": None,
        }
    before_storage, before_rows = row[0], row[1]
    if before_storage != "HEAP":
        return {
            "target_schema": target_schema, "target_table": target_table, "target_column": target_column,
            "status":  "skipped",
            "message": f"[{target_schema}].[{target_table}] is already '{before_storage}'.",
            "command_executed": None,
            "before_metrics":   {"storage_type": before_storage, "row_count": before_rows},
            "after_metrics":    None,
        }

    # ── Pre-check 2: Confirm the candidate key column exists ─────────────────
    cursor.execute("""
        SELECT c.name, TYPE_NAME(c.user_type_id) AS type_name
        FROM sys.columns c
        JOIN sys.tables  t ON t.object_id = c.object_id
        JOIN sys.schemas s ON s.schema_id  = t.schema_id
        WHERE s.name = ? AND t.name = ? AND c.name = ?
    """, target_schema, target_table, target_column)
    col_row = cursor.fetchone()
    if not col_row:
        return {
            "target_schema": target_schema, "target_table": target_table, "target_column": target_column,
            "status":  "failed",
            "message": f"Column [{target_column}] does not exist.",
            "command_executed": None,
            "before_metrics":   {"storage_type": before_storage, "row_count": before_rows},
            "after_metrics":    None,
        }

    # ── Pre-check 3: Permission check ────────────────────────────────────────
    try:
        cursor.execute(
            "SELECT HAS_PERMS_BY_NAME(?, 'OBJECT', 'ALTER'), "
            "       IS_SRVROLEMEMBER('sysadmin')",
            f"{target_schema}.{target_table}"
        )
        has_alter, is_sysadmin = cursor.fetchone()
        if not has_alter and not is_sysadmin:
            return {
                "target_schema": target_schema, "target_table": target_table, "target_column": target_column,
                "status":  "skipped",
                "message": "Current login lacks ALTER TABLE permission.",
                "command_executed": None,
                "before_metrics":   {"storage_type": before_storage, "row_count": before_rows},
                "after_metrics":    None,
            }
    except pyodbc.Error:
        pass  # Proceed cautiously

    # ── Build the index name ───────────────────────
    raw_name   = f"CX_{target_table}_{target_column}"
    index_name = raw_name[:128]
    audit_cmd = f"CREATE CLUSTERED INDEX [{index_name}] ON [{target_schema}].[{target_table}] ([{target_column}])"
    before_metrics = {"storage_type": before_storage, "row_count": before_rows}
    online_used = False

    def _create_index(online: bool) -> None:
        option = "ONLINE = ON" if online else "ONLINE = OFF"
        sql = (
            "DECLARE @sql NVARCHAR(MAX) = N'CREATE CLUSTERED INDEX ' + QUOTENAME(?) + "
            "N' ON ' + QUOTENAME(?) + N'.' + QUOTENAME(?) + N' (' + QUOTENAME(?) + N') WITH (" + option + ")'; "
            "EXEC sp_executesql @sql;"
        )
        cursor.execute(sql, index_name, target_schema, target_table, target_column)

    conn.autocommit = True
    try:
        _create_index(online=True)
        online_used = True
        logger.info("CREATE CLUSTERED INDEX (ONLINE=ON) on [%s].[%s] ([%s]) — success.", target_schema, target_table, target_column)
    except pyodbc.Error as exc:
        err_str = str(exc)
        if "1844" in err_str or "online" in err_str.lower():
            logger.warning("ONLINE=ON not supported on this edition — retrying with ONLINE=OFF.")
            try:
                _create_index(online=False)
                logger.info("CREATE CLUSTERED INDEX (ONLINE=OFF) on [%s].[%s] ([%s]) — success.", target_schema, target_table, target_column)
            except pyodbc.Error:
                conn.autocommit = False
                logger.error("CREATE CLUSTERED INDEX failed (ONLINE=OFF)", exc_info=True)
                return {
                    "target_schema": target_schema, "target_table": target_table, "target_column": target_column,
                    "status": "failed", "message": "Failed with ONLINE=OFF.",
                    "command_executed": audit_cmd + " WITH (ONLINE = OFF)", "before_metrics": before_metrics, "after_metrics": None,
                }
        else:
            conn.autocommit = False
            logger.error("CREATE CLUSTERED INDEX failed", exc_info=True)
            return {
                "target_schema": target_schema, "target_table": target_table, "target_column": target_column,
                "status": "failed", "message": "Failed during creation.",
                "command_executed": audit_cmd + " WITH (ONLINE = ON)", "before_metrics": before_metrics, "after_metrics": None,
            }
    finally:
        conn.autocommit = False

    final_audit_cmd = audit_cmd + (" WITH (ONLINE = ON)" if online_used else " WITH (ONLINE = OFF)")

    # ── Phase 3: Post-execution verification ─────────────────────────────────
    cursor.execute("""
        SELECT i.type_desc, p.rows
        FROM sys.tables t
        JOIN sys.schemas s  ON s.schema_id  = t.schema_id
        JOIN sys.indexes i  ON i.object_id   = t.object_id AND i.name = ?
        JOIN sys.partitions p ON p.object_id  = t.object_id AND p.index_id = i.index_id
        WHERE s.name = ? AND t.name = ?
    """, index_name, target_schema, target_table)
    post_row = cursor.fetchone()

    after_metrics = None
    if post_row:
        after_metrics = {"storage_type": post_row[0], "row_count": post_row[1], "index_name": index_name}

    if after_metrics and after_metrics["storage_type"] == "CLUSTERED":
        return {
            "target_schema": target_schema, "target_table": target_table, "target_column": target_column,
            "status": "success",
            "message": f"Successfully clustered on [{target_column}]." + ("" if online_used else " (Ran OFFLINE)"),
            "command_executed": final_audit_cmd,
            "before_metrics": before_metrics,
            "after_metrics": after_metrics,
        }
    else:
        return {
            "target_schema": target_schema, "target_table": target_table, "target_column": target_column,
            "status": "failed",
            "message": "Post-verification failed.",
            "command_executed": final_audit_cmd,
            "before_metrics": before_metrics,
            "after_metrics": after_metrics,
        }


def execute(
    conn: pyodbc.Connection,
    target_schema: str | None = None,
    target_table:  str | None = None,
    target_column: str | None = None,
) -> dict:
    """
    Create clustered indexes on heap tables to physically re-sort data.
    If specific target schema/table/column are provided, it only processes that table.
    Otherwise, it finds all eligible heaps via analyze() and processes them sequentially.
    """
    targets = []
    if target_schema and target_table and target_column:
        targets.append((target_schema, target_table, target_column))
    else:
        analysis = analyze(conn)
        heaps = [
            h for h in analysis.get("affected_objects", [])
            if h.get("candidate_key") and "manual analysis" not in h["candidate_key"]
        ]
        if not heaps:
            return {
                "status": "skipped",
                "message": "No heap tables with a usable candidate key were found.",
                "results": [],
            }
        for h in heaps:
            targets.append((h["schema"], h["table"], h["candidate_key"]))

    all_results = []
    success_count = 0
    fail_count = 0

    for sch, tbl, col in targets:
        logger.info("Processing heap: [%s].[%s] on [%s]", sch, tbl, col)
        res = _process_single_heap(conn, sch, tbl, col)
        all_results.append(res)
        if res["status"] == "success":
            success_count += 1
        elif res["status"] == "failed":
            fail_count += 1

    overall_status = "success"
    if fail_count > 0:
        overall_status = "partial" if success_count > 0 else "failed"

    if len(targets) == 1:
        overall_msg = all_results[0]["message"]
    else:
        overall_msg = f"Processed {len(targets)} heaps: {success_count} successful, {fail_count} failed."

    return {
        "status": overall_status,
        "message": overall_msg,
        "results": all_results,
    }
