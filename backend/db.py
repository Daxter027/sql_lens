"""
db.py
-----
Low-level pyodbc connection factory. Builds connection strings from session
credentials. Supports both Windows Authentication and SQL Server Authentication.

SECURITY RULES (do not break these):
- The password is NEVER logged at any level.
- The full connection string is NEVER logged.
- Raw pyodbc error messages are NEVER forwarded to the client;
  only sanitised, user-friendly strings are returned.
"""

import pyodbc
import logging
from typing import Optional
from config import QUERY_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)


# Map of pyodbc error substrings → friendly client-facing messages.
# Keep these generic enough to avoid leaking internal details.
_ERROR_MAP = [
    ("Login failed",            "Authentication failed: invalid username or password."),
    ("Cannot open database",    "Database not found or access denied."),
    ("network-related",         "Server not reachable: check server name/IP and firewall."),
    ("Communication link",      "Connection lost: the server closed the connection."),
    ("Timeout expired",         "Connection timed out: server is not responding."),
    ("SSL",                     "SSL/TLS handshake failed: try enabling 'Trust Server Certificate'."),
]


def _sanitise_error(exc: Exception) -> str:
    """Return a safe, generic error string — never the raw pyodbc message."""
    raw = str(exc)
    for fragment, friendly in _ERROR_MAP:
        if fragment.lower() in raw.lower():
            return friendly
    # Fallback: generic, no internal detail
    return "Connection failed: an unexpected error occurred."


def _build_connection_string(
    server: str,
    database: str,
    auth_type: str,
    username: Optional[str],
    password: Optional[str],
    trust_server_certificate: bool = False,
    driver: str = "SQL Server",
) -> str:
    """
    Build a pyodbc connection string. The string is NEVER logged —
    callers must not log it either.
    """
    tsc = "yes" if trust_server_certificate else "no"

    base = (
        f"DRIVER={{{driver}}};"
        f"SERVER={server};"
        f"DATABASE={database};"
        f"TrustServerCertificate={tsc};"
        "Connection Timeout=30;"
    )

    if auth_type == "windows":
        return base + "Trusted_Connection=yes;"
    else:
        return base + f"UID={username};PWD={password};"


def get_connection(
    server: str,
    database: str,
    auth_type: str,
    username: Optional[str] = None,
    password: Optional[str] = None,
    trust_server_certificate: bool = False,
) -> tuple[Optional[pyodbc.Connection], Optional[str]]:
    """
    Create and return a pyodbc connection.

    Returns
    -------
    (connection, None)         on success
    (None, friendly_error_str) on failure
    """
    conn_str = _build_connection_string(
        server, database, auth_type, username, password, trust_server_certificate
    )

    try:
        logger.info(
            "Connecting: server=%s database=%s auth=%s",
            server, database, auth_type
        )
        conn = pyodbc.connect(conn_str, autocommit=False)
        # Per-query timeout safety net so a single slow query can't hang forever.
        conn.timeout = QUERY_TIMEOUT_SECONDS
        logger.info("Connection established: server=%s database=%s", server, database)
        return conn, None

    except pyodbc.InterfaceError as exc:
        return None, _sanitise_error(exc)
    except pyodbc.OperationalError as exc:
        return None, _sanitise_error(exc)
    except pyodbc.Error as exc:
        return None, _sanitise_error(exc)
    except Exception as exc:
        logger.error("Unexpected error during connect (details not forwarded to client)")
        return None, "Connection failed: an unexpected error occurred."


def get_connection_from_session(session) -> tuple[Optional[pyodbc.Connection], Optional[str]]:
    """Convenience wrapper that unpacks a Session object."""
    return get_connection(
        server=session.server,
        database=session.database,
        auth_type=session.auth_type,
        username=session.username,
        password=session.password,
        trust_server_certificate=session.trust_server_certificate,
    )
