"""
transaction_log.py
------------------
Analysis and execution module for Issue 1: Unchecked Transaction Log Growth.

This is the ONLY analysis module in Phase 1 that has a wired execute() function.
The execute() function shrinks the transaction log file after verifying pre-conditions.

IMPORTANT NOTE ON BACKUP TYPES:
  This org runs hourly FULL database backups. Full database backups and
  transaction log backups are DIFFERENT operations with different effects:
    - Full backups: copy all data pages; do NOT truncate the log in FULL recovery
    - Log backups: back up the active log portion; MARK VLFs as reusable so the
      log can be truncated/shrunk
  The safety gate for log shrink depends on LOG backup recency — NOT full backup
  recency. Do not conflate the two when maintaining this code.
"""

from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Any, NamedTuple, Optional
import pyodbc
from config import (
    LOG_BACKUP_THRESHOLD_MINUTES,
    SHRINK_FLOOR_MB,
    SHRINK_HEADROOM_MULTIPLIER,
    VLF_HIGH_COUNT_THRESHOLD,
    LOG_TO_DATA_SIZE_RATIO_THRESHOLD,
)

logger = logging.getLogger(__name__)

ISSUE_ID   = "transaction_log_growth"
ISSUE_NAME = "Unchecked Transaction Log (.ldf) Growth"


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_log_space(cursor) -> dict:
    """
    Get log space metrics.
    Prefers sys.dm_db_log_space_usage (SQL Server 2012+).
    Falls back to DBCC SQLPERF(LOGSPACE) for SQL Server 2008/2005.
    """
    try:
        cursor.execute("""
            SELECT
                total_log_size_mb,
                used_log_space_mb,
                used_log_space_percent,
                (total_log_size_mb - used_log_space_mb) AS log_space_remaining_mb
            FROM sys.dm_db_log_space_usage
        """)
        row = cursor.fetchone()
        return {
            "total_log_size_mb":      round(float(row[0]), 2),
            "used_log_space_mb":      round(float(row[1]), 2),
            "used_log_space_percent": round(float(row[2]), 2),
            "log_space_remaining_mb": round(float(row[3]), 2),
        }
    except pyodbc.Error:
        # Fallback for SQL Server 2008 / pre-2012: use DBCC SQLPERF(LOGSPACE)
        logger.info("sys.dm_db_log_space_usage unavailable — falling back to DBCC SQLPERF(LOGSPACE)")
        cursor.execute("SELECT DB_NAME()")
        current_db = cursor.fetchone()[0]
        cursor.execute("DBCC SQLPERF(LOGSPACE) WITH NO_INFOMSGS")
        for row in cursor.fetchall():
            db_name, log_size_mb, log_used_pct, _status = row[0], float(row[1]), float(row[2]), row[3]
            if db_name.lower() == current_db.lower():
                used_mb = round(log_size_mb * log_used_pct / 100, 2)
                return {
                    "total_log_size_mb":      round(log_size_mb, 2),
                    "used_log_space_mb":      used_mb,
                    "used_log_space_percent": round(log_used_pct, 2),
                    "log_space_remaining_mb": round(log_size_mb - used_mb, 2),
                }
        return {"total_log_size_mb": 0, "used_log_space_mb": 0, "used_log_space_percent": 0, "log_space_remaining_mb": 0}


def _get_db_info(cursor) -> dict:
    """Recovery model, log_reuse_wait_desc, and data size from sys.databases."""
    cursor.execute("""
        SELECT
            d.recovery_model_desc,
            d.log_reuse_wait_desc,
            SUM(mf.size) * 8.0 / 1024 AS data_size_mb
        FROM sys.databases d
        JOIN sys.master_files mf ON mf.database_id = d.database_id
        WHERE d.name = DB_NAME()
          AND mf.type = 0           -- data files only for size comparison
        GROUP BY d.recovery_model_desc, d.log_reuse_wait_desc
    """)
    row = cursor.fetchone()
    if not row:
        return {}
    return {
        "recovery_model":      row[0],
        "log_reuse_wait_desc": row[1],
        "data_size_mb":        round(float(row[2]), 2),
    }


def _get_vlf_count(cursor) -> int:
    """Count VLFs via DBCC LOGINFO."""
    cursor.execute("DBCC LOGINFO() WITH NO_INFOMSGS")
    rows = cursor.fetchall()
    return len(rows)


def _get_last_log_backup(cursor) -> Optional[datetime]:
    """
    Most recent log backup from msdb.dbo.backupset.
    Returns None if no log backup exists or msdb is not accessible.
    NOTE: type = 'L' means transaction log backup — NOT 'D' (full) or 'I' (diff).
    """
    try:
        cursor.execute("""
            SELECT TOP 1 backup_finish_date
            FROM msdb.dbo.backupset
            WHERE database_name = DB_NAME()
              AND type = 'L'                  -- L = transaction log; D = full; I = diff
            ORDER BY backup_finish_date DESC
        """)
        row = cursor.fetchone()
        if row and row[0]:
            dt = row[0]
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        return None
    except pyodbc.Error as exc:
        logger.warning("Could not query msdb for log backups: %s", exc)
        return None


def _get_log_file_info(cursor) -> dict:
    """Logical name and physical info of the log file."""
    cursor.execute("""
        SELECT name, physical_name, size * 8.0 / 1024 AS size_mb
        FROM sys.database_files
        WHERE type = 1   -- 1 = log
    """)
    row = cursor.fetchone()
    if not row:
        return {}
    return {
        "logical_name":   row[0],
        "physical_name":  row[1],
        "size_mb":        round(float(row[2]), 2),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Eligibility check (shared between analyze and pre_check)
# ─────────────────────────────────────────────────────────────────────────────

class EligibilityResult(NamedTuple):
    """Structured result from _check_eligibility()."""
    eligible: bool
    blocking_reason: Optional[str]
    decision_required: bool
    explanation: Optional[str]
    options: Optional[list[dict[str, str]]]


def _check_eligibility(
    recovery_model: str,
    log_reuse_wait_desc: str,
    last_log_backup: Optional[datetime],
) -> EligibilityResult:
    """
    Returns an EligibilityResult indicating whether a log shrink is safe.
    Called both at analysis time and again fresh before execution.
    """
    # Blockers that prevent safe shrink regardless of recovery model
    active_blockers = {
        "ACTIVE_TRANSACTION":         "An active transaction is holding the log open.",
        "ACTIVE_BACKUP_OR_RESTORE":   "A backup or restore is currently running.",
        "REPLICATION":                "Log replication is active — shrink could break replication.",
        "DATABASE_MIRRORING":         "Database mirroring is active.",
        "AVAILABILITY_REPLICA":       "AlwaysOn AG secondary is holding the log.",
    }
    if log_reuse_wait_desc in active_blockers:
        return EligibilityResult(False, active_blockers[log_reuse_wait_desc], False, None, None)

    if recovery_model in ("FULL", "BULK_LOGGED"):
        needs_decision = False
        if last_log_backup is None:
            needs_decision = True
        else:
            now = datetime.now(timezone.utc)
            age_minutes = (now - last_log_backup).total_seconds() / 60
            if age_minutes > LOG_BACKUP_THRESHOLD_MINUTES:
                needs_decision = True
                
        if needs_decision:
            explanation = (
                f"Database is in {recovery_model} recovery, which requires "
                "regular log backups to reclaim log space. None have run recently, which "
                "is why the log keeps growing."
            )
            options = [
                {
                    "id": "switch_simple",
                    "label": "Switch to SIMPLE recovery model",
                    "consequence": "Log will self-manage going forward — this fixes the problem permanently. However, database restores will only be possible to the last full backup; point-in-time recovery to an exact moment will no longer be available. This is a standing change."
                },
                {
                    "id": "nul_backup",
                    "label": "Take a one-time throwaway log backup (BACKUP LOG ... TO DISK = 'NUL')",
                    "consequence": "Clears the log now so it can be shrunk, without changing recovery model. Creates no usable backup file — only marks the log truncatable. Log will grow again unless recurring log backups are set up. Does not reduce point-in-time recovery capability you currently have, since that chain is already broken."
                },
                {
                    "id": "skip",
                    "label": "Don't change anything — skip this optimization for now",
                    "consequence": "No action taken."
                }
            ]
            return EligibilityResult(True, None, True, explanation, options)

    # SIMPLE recovery: no log backups needed, checkpoints handle truncation.
    # If we reach here, no blockers found.
    return EligibilityResult(True, None, False, None, None)


# ─────────────────────────────────────────────────────────────────────────────
# Public: analyze()
# ─────────────────────────────────────────────────────────────────────────────

def analyze(conn: pyodbc.Connection) -> dict[str, Any]:
    """
    Read-only analysis of transaction log health.
    Returns an IssueResult-compatible dict.
    """
    cursor = conn.cursor()

    log_space  = _get_log_space(cursor)
    db_info    = _get_db_info(cursor)
    vlf_count  = _get_vlf_count(cursor)
    log_file   = _get_log_file_info(cursor)

    recovery_model      = db_info.get("recovery_model", "UNKNOWN")
    log_reuse_wait_desc = db_info.get("log_reuse_wait_desc", "NOTHING")
    data_size_mb        = db_info.get("data_size_mb", 0)

    total_log_mb    = log_space.get("total_log_size_mb", 0)
    used_log_mb     = log_space.get("used_log_space_mb", 0)
    used_log_pct    = log_space.get("used_log_space_percent", 0)

    last_log_backup = None
    last_log_backup_str = "Never"
    if recovery_model in ("FULL", "BULK_LOGGED"):
        last_log_backup = _get_last_log_backup(cursor)
        if last_log_backup:
            last_log_backup_str = last_log_backup.strftime("%Y-%m-%d %H:%M UTC")

    elig = _check_eligibility(recovery_model, log_reuse_wait_desc, last_log_backup)
    eligible = elig.eligible
    blocking_reason = elig.blocking_reason
    recovery_decision_required = elig.decision_required
    explanation = elig.explanation
    options = elig.options

    reclaimable_mb = round(total_log_mb - used_log_mb, 2)

    # Severity scoring
    issues_found = []
    if data_size_mb > 0 and total_log_mb > data_size_mb * LOG_TO_DATA_SIZE_RATIO_THRESHOLD:
        issues_found.append(f"Log ({total_log_mb:.0f} MB) is disproportionately large vs data ({data_size_mb:.0f} MB)")
    if vlf_count > VLF_HIGH_COUNT_THRESHOLD:
        issues_found.append(f"High VLF count ({vlf_count}) indicates log fragmentation")
    if total_log_mb > 10_000:
        issues_found.append(f"Log file exceeds 10 GB ({total_log_mb:.0f} MB total)")

    if not issues_found:
        severity = "Low"
        recommended_action = "Transaction log appears healthy. No action required."
        estimated_impact = "N/A"
    elif total_log_mb > 50_000:
        severity = "High"
        recommended_action = (
            f"The transaction log has grown to {total_log_mb:.0f} MB but is only "
            f"{used_log_pct:.1f}% used. Run a log backup (BACKUP LOG) then "
            f"DBCC SHRINKFILE to reclaim ~{reclaimable_mb:.0f} MB. "
            f"Recovery model is {recovery_model}. "
            f"Log reuse is blocked by: {log_reuse_wait_desc}. "
            f"Last log backup: {last_log_backup_str}."
        )
        estimated_impact = f"~{reclaimable_mb:.0f} MB disk space recoverable"
    else:
        severity = "Medium"
        recommended_action = (
            f"Log ({total_log_mb:.0f} MB) is larger than expected. "
            f"Recovery model: {recovery_model}. Last log backup: {last_log_backup_str}."
        )
        estimated_impact = f"~{reclaimable_mb:.0f} MB disk space recoverable"

    return {
        "issue_id":   ISSUE_ID,
        "issue_name": ISSUE_NAME,
        "severity":   severity,
        "affected_objects": [
            {
                "type":          "Log File",
                "logical_name":  log_file.get("logical_name", ""),
                "physical_name": log_file.get("physical_name", ""),
            }
        ],
        "current_metrics": {
            "log_size_mb":           total_log_mb,
            "log_used_mb":           used_log_mb,
            "log_used_pct":          used_log_pct,
            "reclaimable_mb":        reclaimable_mb,
            "vlf_count":             vlf_count,
            "recovery_model":        recovery_model,
            "log_reuse_wait_desc":   log_reuse_wait_desc,
            "last_log_backup":       last_log_backup_str,
            "data_size_mb":          data_size_mb,
        },
        "recommended_action": recommended_action,
        "estimated_impact":   estimated_impact,
        "executable":         True,
        "eligible_for_fix":   eligible,
        "blocking_reason":    blocking_reason,
        "recovery_decision_required": recovery_decision_required,
        "explanation":        explanation,
        "options":            options,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Public: pre_check()  — fresh eligibility re-verification before execution
# ─────────────────────────────────────────────────────────────────────────────

def pre_check(conn: pyodbc.Connection) -> tuple[bool, Optional[str]]:
    """
    Re-run eligibility check from scratch. Called immediately before execute().
    Do NOT rely on the analysis snapshot — time has passed since Step 2.

    Also verifies the current login has ALTER DATABASE permission.

    Returns (ok: bool, reason: str | None)
    """
    cursor = conn.cursor()

    # Re-fetch live state
    db_info    = _get_db_info(cursor)
    recovery_model      = db_info.get("recovery_model", "UNKNOWN")
    log_reuse_wait_desc = db_info.get("log_reuse_wait_desc", "NOTHING")

    last_log_backup = None
    if recovery_model in ("FULL", "BULK_LOGGED"):
        last_log_backup = _get_last_log_backup(cursor)

    elig = _check_eligibility(recovery_model, log_reuse_wait_desc, last_log_backup)
    if not elig.eligible:
        return False, elig.blocking_reason
    if elig.decision_required:
        return False, (
            "Recovery model decision required before proceeding. "
            f"Database is in {recovery_model} recovery with no recent log backups."
        )

    # Permission check — does this login have ALTER permission on the database?
    try:
        cursor.execute(
            "SELECT HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'ALTER'), "
            "       IS_SRVROLEMEMBER('sysadmin')"
        )
        has_alter, is_sysadmin = cursor.fetchone()
        if not has_alter and not is_sysadmin:
            return False, "Current login lacks ALTER DATABASE permission required for DBCC SHRINKFILE."
    except pyodbc.Error:
        pass  # If we can't check, proceed cautiously

    return True, None


# ─────────────────────────────────────────────────────────────────────────────
# Public: execute()
# ─────────────────────────────────────────────────────────────────────────────

def execute(conn: pyodbc.Connection, recovery_choice: Optional[str] = None) -> dict[str, Any]:
    """
    Shrink the transaction log file to a safe target size.

    Steps:
    1. Re-run eligibility check — eligibility must still hold.
    2. Handle recovery model decision if required (switch_simple / nul_backup).
    3. Verify ALTER DATABASE permission.
    4. Capture before-metrics.
    5. Identify log file logical name from sys.database_files (never user input).
    6. Calculate target size: max(used_mb * SHRINK_HEADROOM_MULTIPLIER, SHRINK_FLOOR_MB).
    7. Execute DBCC SHRINKFILE using QUOTENAME-quoted logical name.
    8. Capture after-metrics.
    9. Return structured result.

    The logical name is quoted with QUOTENAME() inside the SQL statement itself
    to prevent any injection from a pathological DB-sourced name.
    """
    cursor = conn.cursor()

    # ── Step 1: Eligibility check ────────────────────────────────────────────
    db_info = _get_db_info(cursor)
    recovery_model = db_info.get("recovery_model", "UNKNOWN")
    log_reuse_wait_desc = db_info.get("log_reuse_wait_desc", "NOTHING")
    last_log_backup = None
    if recovery_model in ("FULL", "BULK_LOGGED"):
        last_log_backup = _get_last_log_backup(cursor)

    elig = _check_eligibility(recovery_model, log_reuse_wait_desc, last_log_backup)

    if not elig.eligible:
        return {
            "status":           "skipped",
            "message":          f"Pre-execution check failed: {elig.blocking_reason}",
            "command_executed": None,
            "before_metrics":   None,
            "after_metrics":    None,
            "recovery_choice":  recovery_choice,
        }

    # ── Step 2: Handle recovery decision if required ─────────────────────────
    if elig.decision_required:
        if recovery_choice == "skip" or not recovery_choice:
            return {
                "status":           "skipped",
                "message":          "Optimization skipped by user choice.",
                "command_executed": None,
                "before_metrics":   None,
                "after_metrics":    None,
                "recovery_choice":  recovery_choice,
            }
        elif recovery_choice == "switch_simple":
            try:
                conn.autocommit = True
                cursor.execute(
                    "DECLARE @db NVARCHAR(128) = DB_NAME(); "
                    "DECLARE @sql NVARCHAR(MAX) = N'ALTER DATABASE ' + QUOTENAME(@db) + N' SET RECOVERY SIMPLE'; "
                    "EXEC sp_executesql @sql;"
                )
                logger.info("Switched recovery model to SIMPLE.")
            except pyodbc.Error as exc:
                return {
                    "status": "failed",
                    "message": "Failed to switch recovery model to SIMPLE.",
                    "command_executed": "ALTER DATABASE [DB] SET RECOVERY SIMPLE",
                    "before_metrics": None,
                    "after_metrics": None,
                    "recovery_choice": recovery_choice,
                }
            finally:
                conn.autocommit = False
        elif recovery_choice == "nul_backup":
            try:
                conn.autocommit = True
                cursor.execute(
                    "DECLARE @db NVARCHAR(128) = DB_NAME(); "
                    "DECLARE @sql NVARCHAR(MAX) = N'BACKUP LOG ' + QUOTENAME(@db) + N' TO DISK = ''NUL'''; "
                    "EXEC sp_executesql @sql;"
                )
                logger.info("Took throwaway NUL log backup.")
            except pyodbc.Error as exc:
                return {
                    "status": "failed",
                    "message": "Failed to take NUL log backup.",
                    "command_executed": "BACKUP LOG [DB] TO DISK = 'NUL'",
                    "before_metrics": None,
                    "after_metrics": None,
                    "recovery_choice": recovery_choice,
                }
            finally:
                conn.autocommit = False

    # ── Step 3: Verify ALTER DATABASE permission ─────────────────────────────
    try:
        cursor.execute(
            "SELECT HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'ALTER'), "
            "       IS_SRVROLEMEMBER('sysadmin')"
        )
        has_alter, is_sysadmin = cursor.fetchone()
        if not has_alter and not is_sysadmin:
            return {
                "status": "skipped",
                "message": "Current login lacks ALTER DATABASE permission required for DBCC SHRINKFILE.",
                "command_executed": None,
                "before_metrics": None,
                "after_metrics": None,
                "recovery_choice": recovery_choice,
            }
    except pyodbc.Error:
        pass  # If we can't check, proceed cautiously

    # ── Step 4: Before metrics ───────────────────────────────────────────────
    before = _get_log_space(cursor)
    before_vlf = _get_vlf_count(cursor)

    # ── Step 5: Log file logical name ────────────────────────────────────────
    log_file = _get_log_file_info(cursor)
    logical_name = log_file.get("logical_name")
    if not logical_name:
        return {
            "status":  "failed",
            "message": "Could not determine log file logical name from sys.database_files.",
            "command_executed": None,
            "before_metrics": {
                "log_size_mb": before.get("total_log_size_mb", 0),
                "log_used_mb": before.get("used_log_space_mb", 0),
                "log_used_pct": before.get("used_log_space_percent", 0),
                "vlf_count": before_vlf,
            },
            "after_metrics": None,
            "recovery_choice": recovery_choice,
        }

    # ── Step 6: Target size ──────────────────────────────────────────────────
    used_mb = before.get("used_log_space_mb", 0)
    target_mb = max(
        int(used_mb * SHRINK_HEADROOM_MULTIPLIER),
        SHRINK_FLOOR_MB
    )

    # ── Step 7: Execute DBCC SHRINKFILE ─────────────────────────────────────
    # QUOTENAME wraps the logical name in square brackets, neutralising any
    # special characters even though this value comes from a DMV, not user input.
    shrink_sql = """
        DECLARE @ln NVARCHAR(128) = ?;
        DECLARE @sz INT = ?;
        DECLARE @sql NVARCHAR(MAX) = N'DBCC SHRINKFILE(' + QUOTENAME(@ln) + N', ' + CAST(@sz AS NVARCHAR(10)) + N') WITH NO_INFOMSGS';
        EXEC sp_executesql @sql;
    """
    audit_cmd  = f"DBCC SHRINKFILE(QUOTENAME('{logical_name}'), {target_mb})"

    logger.info(
        "Executing DBCC SHRINKFILE: logical_name=%s target_mb=%d",
        logical_name, target_mb
    )

    try:
        conn.autocommit = True  # DBCC requires autocommit
        cursor.execute(shrink_sql, logical_name, target_mb)
    except pyodbc.Error as exc:
        logger.error("DBCC SHRINKFILE failed (details not forwarded to client)", exc_info=True)
        return {
            "status":           "failed",
            "message":          "DBCC SHRINKFILE failed. Check SQL Server error logs for details.",
            "command_executed": audit_cmd,
            "before_metrics": {
                "log_size_mb": before.get("total_log_size_mb", 0),
                "log_used_mb": before.get("used_log_space_mb", 0),
                "log_used_pct": before.get("used_log_space_percent", 0),
                "vlf_count": before_vlf,
            },
            "after_metrics":    None,
            "recovery_choice":  recovery_choice,
        }
    finally:
        conn.autocommit = False

    # ── Step 8: After metrics ────────────────────────────────────────────────
    after     = _get_log_space(cursor)
    after_vlf = _get_vlf_count(cursor)

    delta = round(before["total_log_size_mb"] - after["total_log_size_mb"], 2)

    return {
        "status": "success",
        "message": f"Log file shrunk successfully. Freed ~{delta:.1f} MB.",
        "command_executed": audit_cmd,
        "before_metrics": {
            "log_size_mb": before.get("total_log_size_mb", 0),
            "log_used_mb": before.get("used_log_space_mb", 0),
            "log_used_pct": before.get("used_log_space_percent", 0),
            "vlf_count": before_vlf,
        },
        "after_metrics": {
            "log_size_mb": after.get("total_log_size_mb", 0),
            "log_used_mb": after.get("used_log_space_mb", 0),
            "log_used_pct": after.get("used_log_space_percent", 0),
            "vlf_count": after_vlf,
        },
        "delta_mb_freed": delta,
        "recovery_choice": recovery_choice,
    }
