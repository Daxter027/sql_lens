"""
connection.py
-------------
Provides a reusable function to obtain a pyodbc connection to Microsoft SQL Server
using Windows Authentication (Trusted_Connection).

Usage:
    from connection import get_connection

    conn = get_connection()
    if conn:
        cursor = conn.cursor()
        ...
        conn.close()
"""

import pyodbc
import logging

# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Connection defaults  –  edit these to match your environment
# ---------------------------------------------------------------------------
_DEFAULT_SERVER   = "localhost"
_DEFAULT_DATABASE = "welingkarlivelatest"
_DEFAULT_DRIVER   = "SQL Server"   # use "ODBC Driver 17 for SQL Server" if available


def get_connection(
    server: str   = _DEFAULT_SERVER,
    database: str = _DEFAULT_DATABASE,
    driver: str   = _DEFAULT_DRIVER,
) -> pyodbc.Connection | None:
    """
    Create and return an active pyodbc connection to SQL Server.

    Parameters
    ----------
    server   : SQL Server host name or IP address (default: localhost).
    database : Target database name.
    driver   : ODBC driver name registered on this machine.

    Returns
    -------
    pyodbc.Connection on success, or None on failure.
    """
    connection_string = (
        f"DRIVER={{{driver}}};"
        f"SERVER={server};"
        f"DATABASE={database};"
        "Trusted_Connection=yes;"      # Windows Authentication
        "Connection Timeout=30;"
    )

    try:
        logger.info("Connecting to [%s].[%s] …", server, database)
        conn = pyodbc.connect(connection_string, autocommit=False)
        logger.info("Connection established successfully.")
        return conn

    except pyodbc.InterfaceError as exc:
        logger.error("Driver / interface error: %s", exc)
    except pyodbc.OperationalError as exc:
        logger.error("Operational error (server unreachable or login failed): %s", exc)
    except pyodbc.Error as exc:
        logger.error("Unexpected pyodbc error: %s", exc)

    return None


# ---------------------------------------------------------------------------
# Quick smoke-test when run directly
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    conn = get_connection()
    if conn:
        cursor = conn.cursor()
        cursor.execute("SELECT DB_NAME(), @@SERVERNAME, GETDATE()")
        db_name, server_name, ts = cursor.fetchone()
        print(f"  Database : {db_name}")
        print(f"  Server   : {server_name}")
        print(f"  Timestamp: {ts}")
        conn.close()
        logger.info("Connection closed.")
    else:
        logger.error("Could not establish a database connection.")
