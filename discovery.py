"""
discovery.py
------------
Collects key metadata from a SQL Server database and returns it as a
plain Python dictionary.

Collected fields
----------------
  database_name   : Name of the connected database
  sql_version     : Full SQL Server version string
  recovery_model  : SIMPLE | BULK_LOGGED | FULL
  data_size_mb    : Total data (.mdf / .ndf) size in MB
  log_size_mb     : Total log (.ldf) size in MB
  table_count     : Number of user tables in the database

Usage
-----
    from discovery import discover

    info = discover()
    for key, value in info.items():
        print(f"{key}: {value}")
"""

import logging
from connection import get_connection

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Individual query helpers
# ---------------------------------------------------------------------------

def _fetch_one(cursor, sql: str):
    """Execute *sql* and return the first column of the first row."""
    cursor.execute(sql)
    row = cursor.fetchone()
    return row[0] if row else None


def _get_database_name(cursor) -> str:
    return _fetch_one(cursor, "SELECT DB_NAME()") or "unknown"


def _get_sql_version(cursor) -> str:
    return _fetch_one(cursor, "SELECT @@VERSION") or "unknown"


def _get_recovery_model(cursor, db_name: str) -> str:
    sql = """
        SELECT recovery_model_desc
        FROM   sys.databases
        WHERE  name = ?
    """
    cursor.execute(sql, db_name)
    row = cursor.fetchone()
    return row[0] if row else "unknown"


def _get_file_sizes(cursor) -> tuple[float, float]:
    """
    Returns (data_size_mb, log_size_mb) by querying sys.database_files.
    type_desc = 'ROWS' → data files
    type_desc = 'LOG'  → log files
    size is measured in 8-KB pages; convert to MB.
    """
    sql = """
        SELECT
            type_desc,
            SUM(CAST(size AS BIGINT) * 8.0 / 1024) AS size_mb
        FROM sys.database_files
        GROUP BY type_desc
    """
    cursor.execute(sql)
    data_mb = 0.0
    log_mb  = 0.0
    for type_desc, size_mb in cursor.fetchall():
        if type_desc == "ROWS":
            data_mb = round(float(size_mb), 2)
        elif type_desc == "LOG":
            log_mb = round(float(size_mb), 2)
    return data_mb, log_mb


def _get_table_count(cursor) -> int:
    sql = """
        SELECT COUNT(*)
        FROM   sys.tables
        WHERE  type = 'U'          -- user tables only
    """
    return _fetch_one(cursor, sql) or 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def discover(
    server:   str = None,
    database: str = None,
    driver:   str = None,
) -> dict:
    """
    Connect to SQL Server and return a metadata dictionary.

    Parameters
    ----------
    server, database, driver  – forwarded to connection.get_connection().
                                Pass None to use the module defaults.

    Returns
    -------
    dict with the collected fields, or an empty dict on connection failure.
    """
    # Build kwargs only for values the caller explicitly supplied
    kwargs = {k: v for k, v in
              dict(server=server, database=database, driver=driver).items()
              if v is not None}

    conn = get_connection(**kwargs)
    if conn is None:
        logger.error("discovery: could not obtain a database connection.")
        return {}

    result: dict = {}
    try:
        cursor = conn.cursor()

        db_name = _get_database_name(cursor)
        data_mb, log_mb = _get_file_sizes(cursor)

        result = {
            "database_name" : db_name,
            "sql_version"   : _get_sql_version(cursor),
            "recovery_model": _get_recovery_model(cursor, db_name),
            "data_size_mb"  : data_mb,
            "log_size_mb"   : log_mb,
            "table_count"   : _get_table_count(cursor),
        }

        logger.info("Discovery complete for database '%s'.", db_name)

    except Exception as exc:
        logger.error("Discovery failed: %s", exc)

    finally:
        conn.close()
        logger.info("Connection closed.")

    return result


# ---------------------------------------------------------------------------
# Quick smoke-test when run directly
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json
    info = discover()
    if info:
        print("\n=== Database Discovery Report ===")
        print(json.dumps(info, indent=4, default=str))
    else:
        print("No data collected — check connection settings and logs.")
