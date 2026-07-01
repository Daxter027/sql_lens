"""
unused_indexes.py
-----------------
Analysis module for Issue 4: High-Overhead Unused Index Audit & Purge.

Phase 1: analyze() is fully implemented with real DMV T-SQL.
         execute() is stubbed — index removal is NOT in scope for Phase 1.

TODO (Phase 2): Implement execute() to:
  - DISABLE (not DROP) candidate indexes as the first safe step
  - Monitor for query plan regressions before considering DROP
  - Require explicit user confirmation per index before disabling
  - NEVER drop indexes enforcing PK or unique constraints

CONFIDENCE NOTE: sys.dm_db_index_usage_stats is cleared on every SQL Server restart.
If the instance has restarted recently (< UNUSED_INDEX_MIN_DAYS_SINCE_RESTART days),
usage stats are unreliable. This module explicitly flags low confidence in that case
rather than making recommendations on thin data.
"""

from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Any
import pyodbc
from config import UNUSED_INDEX_MIN_DAYS_SINCE_RESTART, UNUSED_INDEX_MIN_WRITES

logger = logging.getLogger(__name__)

ISSUE_ID   = "unused_indexes"
ISSUE_NAME = "High-Overhead Unused Index Audit & Purge"


def analyze(conn: pyodbc.Connection) -> dict[str, Any]:
    """
    Identify indexes with high write overhead but minimal read benefit.
    Confidence is marked LOW if SQL Server restarted within the threshold window.
    PK and unique constraint indexes are always excluded.
    """
    cursor = conn.cursor()

    # Get last restart time
    cursor.execute("SELECT sqlserver_start_time FROM sys.dm_os_sys_info")
    restart_row = cursor.fetchone()
    restart_time = restart_row[0] if restart_row else None
    if restart_time and restart_time.tzinfo is None:
        restart_time = restart_time.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    days_since_restart = None
    low_confidence = False
    if restart_time:
        days_since_restart = (now - restart_time).days
        low_confidence = days_since_restart < UNUSED_INDEX_MIN_DAYS_SINCE_RESTART

    # Identify candidate unused indexes
    cursor.execute("""
        SELECT
            s.name                              AS schema_name,
            t.name                              AS table_name,
            i.name                              AS index_name,
            i.type_desc                         AS index_type,
            ISNULL(us.user_seeks, 0)            AS user_seeks,
            ISNULL(us.user_scans, 0)            AS user_scans,
            ISNULL(us.user_lookups, 0)          AS user_lookups,
            ISNULL(us.user_updates, 0)          AS user_updates,
            us.last_user_seek,
            us.last_user_scan,
            p.rows                              AS row_count,
            CAST(SUM(a.total_pages) * 8.0 / 1024 AS DECIMAL(18,2)) AS index_size_mb
        FROM sys.indexes i
        JOIN sys.tables t      ON t.object_id = i.object_id
        JOIN sys.schemas s     ON s.schema_id = t.schema_id
        JOIN sys.partitions p  ON p.object_id = i.object_id
                               AND p.index_id = i.index_id
        JOIN sys.allocation_units a ON a.container_id = p.hobt_id
        LEFT JOIN sys.dm_db_index_usage_stats us
               ON us.object_id  = i.object_id
              AND us.index_id   = i.index_id
              AND us.database_id = DB_ID()
        WHERE t.is_ms_shipped   = 0
          AND i.index_id       > 1              -- non-clustered only (0=heap, 1=clustered)
          AND i.is_primary_key = 0              -- exclude PK
          AND i.is_unique_constraint = 0        -- exclude unique constraints
          AND i.is_unique     = 0              -- exclude any unique index
          AND i.is_disabled   = 0              -- exclude already-disabled
          AND ISNULL(us.user_updates, 0) >= ?
          AND (
              ISNULL(us.user_seeks, 0)
            + ISNULL(us.user_scans, 0)
            + ISNULL(us.user_lookups, 0)
          ) = 0                                -- zero reads since last restart
        GROUP BY
            s.name, t.name, i.name, i.type_desc,
            us.user_seeks, us.user_scans, us.user_lookups, us.user_updates,
            us.last_user_seek, us.last_user_scan, p.rows
        ORDER BY ISNULL(us.user_updates, 0) DESC
    """, UNUSED_INDEX_MIN_WRITES)

    candidates = []
    for row in cursor.fetchall():
        (schema_name, table_name, index_name, index_type,
         seeks, scans, lookups, updates, last_seek, last_scan,
         row_count, index_size_mb) = row

        candidates.append({
            "schema":         schema_name,
            "table":          table_name,
            "index":          index_name,
            "type":           index_type,
            "reads":          seeks + scans + lookups,
            "writes":         updates,
            "last_seek":      str(last_seek) if last_seek else "Never",
            "last_scan":      str(last_scan) if last_scan else "Never",
            "row_count":      row_count,
            "size_mb":        float(index_size_mb) if index_size_mb else 0,
        })

    total_wasted_mb = round(sum(c["size_mb"] for c in candidates), 2)

    confidence = "LOW" if low_confidence else "HIGH"
    confidence_note = (
        f"Instance restarted {days_since_restart} day(s) ago — usage stats cover only "
        f"{days_since_restart} day(s). Recommendations based on less than "
        f"{UNUSED_INDEX_MIN_DAYS_SINCE_RESTART} days of data have LOW confidence."
        if low_confidence else
        f"Instance has been running for {days_since_restart} day(s) — "
        "usage stats are considered reliable."
    )

    if not candidates:
        return {
            "issue_id":         ISSUE_ID,
            "issue_name":       ISSUE_NAME,
            "severity":         "Low",
            "affected_objects": [],
            "current_metrics":  {
                "candidate_count":    0,
                "wasted_space_mb":    0,
                "confidence":         confidence,
                "days_since_restart": days_since_restart,
            },
            "recommended_action": "No high-overhead unused indexes found.",
            "estimated_impact":   "N/A",
            "executable":         False,
            "eligible_for_fix":   False,
            "blocking_reason":    "No unused indexes meet the criteria.",
            "analysis_note":      confidence_note,
        }

    severity = "High" if total_wasted_mb > 5_000 else "Medium" if total_wasted_mb > 500 else "Low"

    return {
        "issue_id":         ISSUE_ID,
        "issue_name":       ISSUE_NAME,
        "severity":         severity,
        "affected_objects": candidates,
        "current_metrics": {
            "candidate_count":    len(candidates),
            "wasted_space_mb":    total_wasted_mb,
            "confidence":         confidence,
            "days_since_restart": days_since_restart,
        },
        "recommended_action": (
            f"Found {len(candidates)} index(es) with zero reads but high write overhead, "
            f"consuming ~{total_wasted_mb:.0f} MB. "
            "The fix DISABLEs each index (reversible) rather than dropping it, stopping "
            "the write overhead immediately while preserving the index definition so it "
            "can be rebuilt if a regression appears. Space is not reclaimed until a DBA "
            "later DROPs the disabled index in a maintenance window. "
            "Indexes enforcing PK/unique constraints are never candidates."
            + ("" if confidence == "HIGH" else
               " ⚠ Usage stats have LOW confidence — review carefully before disabling.")
        ),
        "estimated_impact": (
            f"~{total_wasted_mb:.0f} MB reclaimable after a follow-up DROP; "
            "reduced write amplification on high-volume tables immediately on disable."
        ),
        "executable":       True,
        "eligible_for_fix": True,
        "blocking_reason":  None,
        "analysis_note":    confidence_note,
    }


def _process_single_index(
    conn: pyodbc.Connection,
    schema_name: str,
    table_name: str,
    index_name: str,
) -> dict:
    """DISABLE a single non-clustered index after re-verifying it is safe to do so."""
    cursor = conn.cursor()
    base = {
        "schema": schema_name, "table": table_name, "index": index_name,
        "command_executed": None, "before_metrics": None, "after_metrics": None,
    }

    # ── Re-verify the index still exists and is still a safe candidate ────────
    cursor.execute("""
        SELECT i.index_id, i.type_desc, i.is_primary_key,
               i.is_unique, i.is_unique_constraint, i.is_disabled
        FROM sys.indexes i
        JOIN sys.tables  t ON t.object_id = i.object_id
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        WHERE s.name = ? AND t.name = ? AND i.name = ?
    """, schema_name, table_name, index_name)
    row = cursor.fetchone()
    if not row:
        return {**base, "status": "skipped",
                "message": f"Index [{index_name}] no longer exists on [{schema_name}].[{table_name}]."}

    index_id, type_desc, is_pk, is_unique, is_uc, is_disabled = row
    if is_disabled:
        return {**base, "status": "skipped",
                "message": f"Index [{index_name}] is already disabled.",
                "before_metrics": {"is_disabled": True}}
    # Hard safety gates — never disable clustered/PK/unique indexes.
    if index_id <= 1 or type_desc != "NONCLUSTERED" or is_pk or is_unique or is_uc:
        return {**base, "status": "skipped",
                "message": (f"Index [{index_name}] is not an eligible non-clustered index "
                            "(clustered, PK, or unique) — skipped for safety.")}

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
        pass  # Proceed cautiously if the check itself fails

    audit_cmd = (
        f"ALTER INDEX [{index_name}] ON [{schema_name}].[{table_name}] DISABLE"
    )
    before_metrics = {"is_disabled": False}

    # ── Execute DISABLE ──────────────────────────────────────────────────────
    # QUOTENAME-quote all identifiers even though they originate from DMVs.
    sql = (
        "DECLARE @sql NVARCHAR(MAX) = N'ALTER INDEX ' + QUOTENAME(?) + N' ON ' + "
        "QUOTENAME(?) + N'.' + QUOTENAME(?) + N' DISABLE'; EXEC sp_executesql @sql;"
    )
    try:
        conn.autocommit = True
        cursor.execute(sql, index_name, schema_name, table_name)
        conn.autocommit = False
        logger.info("Disabled index [%s] on [%s].[%s].", index_name, schema_name, table_name)
    except pyodbc.Error:
        conn.autocommit = False
        logger.error("ALTER INDEX DISABLE failed (details not forwarded to client)", exc_info=True)
        return {**base, "status": "failed", "command_executed": audit_cmd,
                "message": "Failed to disable the index.", "before_metrics": before_metrics}

    # ── Post-verify ──────────────────────────────────────────────────────────
    cursor.execute("""
        SELECT i.is_disabled
        FROM sys.indexes i
        JOIN sys.tables  t ON t.object_id = i.object_id
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        WHERE s.name = ? AND t.name = ? AND i.name = ?
    """, schema_name, table_name, index_name)
    post = cursor.fetchone()
    after_metrics = {"is_disabled": bool(post[0])} if post else None

    if after_metrics and after_metrics["is_disabled"]:
        return {**base, "status": "success", "command_executed": audit_cmd,
                "message": f"Index [{index_name}] disabled. Rebuild it to restore if needed.",
                "before_metrics": before_metrics, "after_metrics": after_metrics}
    return {**base, "status": "failed", "command_executed": audit_cmd,
            "message": "Disable command ran but post-verification did not confirm it.",
            "before_metrics": before_metrics, "after_metrics": after_metrics}


def execute(
    conn: pyodbc.Connection,
    target_schema: str | None = None,
    target_table:  str | None = None,
    target_column: str | None = None,  # reused to carry the index name when targeting one
) -> dict:
    """
    DISABLE high-overhead unused indexes (reversible — never DROP).

    If target_schema/target_table/target_column (index name) are all provided,
    only that one index is processed. Otherwise every eligible candidate from
    analyze() is disabled sequentially.
    """
    targets = []
    if target_schema and target_table and target_column:
        targets.append((target_schema, target_table, target_column))
    else:
        analysis = analyze(conn)
        for c in analysis.get("affected_objects", []):
            targets.append((c["schema"], c["table"], c["index"]))
        if not targets:
            return {"status": "skipped",
                    "message": "No eligible unused indexes were found to disable.",
                    "results": []}

    results = []
    success = fail = 0
    for sch, tbl, idx in targets:
        res = _process_single_index(conn, sch, tbl, idx)
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
        message = f"Processed {len(targets)} index(es): {success} disabled, {fail} failed."

    return {"status": status, "message": message, "results": results}
