"""
transaction_log_detector.py
---------------------------
Executes DBCC SQLPERF(LOGSPACE) for the connected database and analyses
whether the transaction log has grown to a problematic size.

Returned dictionary fields
--------------------------
  database_name          : Name of the database analysed
  log_size_mb            : Total allocated log size (MB)
  log_used_pct           : Percentage of log currently used
  log_used_mb            : Used portion of the log (MB)
  reclaimable_mb         : Estimated space that could be freed by a log backup
  problem                : Short problem description (or "None" if healthy)
  severity               : CRITICAL | WARNING | INFO | OK
  recommendation         : Suggested action

Severity thresholds
-------------------
  CRITICAL  : log_used_pct >= 80 %
  WARNING   : log_used_pct >= 50 %  OR  log_size_mb >= 10 000 MB
  INFO      : log_size_mb >= 1 000 MB  (large but not critically used)
  OK        : everything within normal bounds
"""

import logging
from connection import get_connection

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thresholds (easily tunable)
# ---------------------------------------------------------------------------
CRITICAL_USED_PCT   = 80.0    # % log used  → CRITICAL
WARNING_USED_PCT    = 50.0    # % log used  → WARNING
WARNING_SIZE_MB     = 10_000  # MB total    → WARNING regardless of usage
INFO_SIZE_MB        =  1_000  # MB total    → INFO


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _run_sqlperf(cursor, target_db: str) -> dict | None:
    """
    Execute DBCC SQLPERF(LOGSPACE) and return the row that matches
    *target_db*, or None if not found.

    Columns returned by SQL Server 2008+:
        Database Name | Log Size (MB) | Log Space Used (%) | Status
    """
    cursor.execute("DBCC SQLPERF(LOGSPACE) WITH NO_INFOMSGS")
    for row in cursor.fetchall():
        db_name, log_size_mb, log_used_pct, status = (
            row[0], float(row[1]), float(row[2]), row[3]
        )
        if db_name.lower() == target_db.lower():
            return {
                "db_name"      : db_name,
                "log_size_mb"  : round(log_size_mb, 2),
                "log_used_pct" : round(log_used_pct, 2),
            }
    return None


def _classify(log_size_mb: float, log_used_pct: float) -> tuple[str, str, str]:
    """
    Returns (problem, severity, recommendation) based on thresholds.
    """
    if log_used_pct >= CRITICAL_USED_PCT:
        return (
            "Transaction log is critically full",
            "CRITICAL",
            "Take an immediate log backup (BACKUP LOG) or switch to SIMPLE "
            "recovery model if point-in-time recovery is not required. "
            "Investigate long-running or open transactions.",
        )

    if log_used_pct >= WARNING_USED_PCT:
        return (
            "Transaction log usage is elevated",
            "WARNING",
            "Schedule more frequent log backups. Review open transactions "
            "and ensure log backups are not failing.",
        )

    if log_size_mb >= WARNING_SIZE_MB:
        return (
            "Transaction log is very large in absolute size",
            "WARNING",
            "Although current usage is low, the log has grown significantly. "
            "Consider shrinking after a log backup if the space is no longer "
            "needed (DBCC SHRINKFILE).",
        )

    if log_size_mb >= INFO_SIZE_MB:
        return (
            "Transaction log size is notable but usage is within limits",
            "INFO",
            "Monitor growth trends. Ensure regular log backups are in place.",
        )

    return (
        "None",
        "OK",
        "No action required. Continue regular log backup schedule.",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_transaction_log_issue(
    server:   str = None,
    database: str = None,
    driver:   str = None,
) -> dict:
    """
    Connect to SQL Server, run DBCC SQLPERF(LOGSPACE), and return a
    structured analysis dictionary for the target database.

    Parameters
    ----------
    server, database, driver  – forwarded to connection.get_connection().
                                Pass None to use the module defaults.

    Returns
    -------
    dict  – analysis result, or empty dict on connection/query failure.
    """
    kwargs = {k: v for k, v in
              dict(server=server, database=database, driver=driver).items()
              if v is not None}

    conn = get_connection(**kwargs)
    if conn is None:
        logger.error("transaction_log_detector: could not obtain a connection.")
        return {}

    result: dict = {}
    try:
        cursor = conn.cursor()

        # Identify the current database
        cursor.execute("SELECT DB_NAME()")
        current_db = cursor.fetchone()[0]

        perf = _run_sqlperf(cursor, current_db)
        if perf is None:
            logger.warning(
                "DBCC SQLPERF(LOGSPACE) returned no row for '%s'.", current_db
            )
            return {}

        log_size_mb  = perf["log_size_mb"]
        log_used_pct = perf["log_used_pct"]
        log_used_mb  = round(log_size_mb * log_used_pct / 100, 2)
        reclaimable  = round(log_size_mb - log_used_mb, 2)

        problem, severity, recommendation = _classify(log_size_mb, log_used_pct)

        result = {
            "database_name"   : perf["db_name"],
            "log_size_mb"     : log_size_mb,
            "log_used_pct"    : log_used_pct,
            "log_used_mb"     : log_used_mb,
            "reclaimable_mb"  : reclaimable,
            "problem"         : problem,
            "severity"        : severity,
            "recommendation"  : recommendation,
        }

        logger.info(
            "Log analysis complete — severity: %s  (%.1f MB used / %.1f MB total)",
            severity, log_used_mb, log_size_mb,
        )

    except Exception as exc:
        logger.error("transaction_log_detector error: %s", exc)

    finally:
        conn.close()
        logger.info("Connection closed.")

    return result


# ---------------------------------------------------------------------------
# Quick smoke-test when run directly
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json

    result = detect_transaction_log_issue()
    if result:
        print("\n=== Transaction Log Analysis ===")
        print(json.dumps(result, indent=4, default=str))
    else:
        print("No data returned — check connection settings and logs.")
