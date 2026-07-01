"""
routers/connect.py
------------------
Handles database connection establishment and teardown.

SECURITY CONTRACT:
- Password is accepted only to build a pyodbc connection string — never logged,
  never stored anywhere outside the session store, never echoed to the client.
- All connection errors are sanitised before reaching the client.
- Only a session token (UUID) is returned to the client.
"""

from fastapi import APIRouter, HTTPException, Request, Header, Query
from models import ConnectRequest, ConnectResponse
from db import get_connection, get_connection_from_session
from session import store
import logging
import traceback

import pyodbc

# User databases the login can actually open, ONLINE, excluding the 4 system DBs
# (master/tempdb/model/msdb have database_id 1-4).
_LIST_DATABASES_SQL = """
    SELECT name
    FROM sys.databases
    WHERE database_id > 4
      AND state = 0                 -- ONLINE
      AND HAS_DBACCESS(name) = 1    -- login can open it
    ORDER BY name
"""

logger = logging.getLogger(__name__)
router = APIRouter()



@router.post("/connect", response_model=ConnectResponse)
async def connect(req: ConnectRequest):
    """
    Validate connection credentials and create a server-side session.
    Returns a session token — credentials are never returned to the client.
    """
    try:
        if req.auth_type == "sql" and (not req.username or not req.password):
            raise HTTPException(
                status_code=400,
                detail="Username and password are required for SQL Server authentication."
            )

        conn, error = get_connection(
            server=req.server,
            database=req.database,
            auth_type=req.auth_type,
            username=req.username,
            password=req.password,
            trust_server_certificate=req.trust_server_certificate,
        )

        if error:
            raise HTTPException(status_code=401, detail=error)

        # Validate with a lightweight query and resolve the database we actually
        # landed on (the login's default when none was requested).
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT DB_NAME()")
            resolved_db = cursor.fetchone()[0]
        except Exception as qe:
            conn.close()
            logger.error("Test query failed: %s", qe)
            raise HTTPException(
                status_code=401,
                detail="Connection succeeded but test query failed. Check database permissions."
            )
        finally:
            conn.close()

        token = store.create(
            server=req.server,
            database=resolved_db,
            auth_type=req.auth_type,
            username=req.username,
            password=req.password,
            trust_server_certificate=req.trust_server_certificate,
        )

        return ConnectResponse(
            session_token=token,
            server=req.server,
            database=resolved_db,
        )

    except HTTPException:
        raise  # let FastAPI handle these normally
    except Exception:
        # Log the FULL traceback so we can see exactly what went wrong
        logger.error("Unexpected error in /api/connect:\n%s", traceback.format_exc())
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred. Check server logs for details."
        )


@router.get("/databases")
async def list_databases(x_session_token: str = Header(..., alias="X-Session-Token")):
    """
    List the databases available on the connected server, using the session's
    stored credentials (so the client never re-sends them). Used by the in-app
    database switcher.
    """
    session = store.get(x_session_token)
    if not session:
        raise HTTPException(status_code=401, detail="Session not found or expired. Please reconnect.")

    conn, error = get_connection_from_session(session)
    if error:
        raise HTTPException(status_code=400, detail=error)
    try:
        cursor = conn.cursor()
        cursor.execute(_LIST_DATABASES_SQL)
        databases = [row[0] for row in cursor.fetchall()]
    except pyodbc.Error:
        logger.error("Failed to list databases", exc_info=True)
        raise HTTPException(status_code=400, detail="Could not list databases on this server.")
    finally:
        conn.close()

    return {"server": session.server, "current": session.database, "databases": databases}


@router.post("/switch-database")
async def switch_database(
    x_session_token: str = Header(..., alias="X-Session-Token"),
    database: str = Query(..., description="Target database on the same server"),
):
    """
    Re-point the current session at a different database on the SAME server,
    reusing the stored credentials. Validates connectivity before switching, and
    clears the session's cached analysis/execution so the report never mixes DBs.
    """
    session = store.get(x_session_token)
    if not session:
        raise HTTPException(status_code=401, detail="Session not found or expired. Please reconnect.")

    if database == session.database:
        return {"server": session.server, "database": session.database}

    # Validate the login can actually open the target DB before committing.
    conn, error = get_connection(
        server=session.server,
        database=database,
        auth_type=session.auth_type,
        username=session.username,
        password=session.password,
        trust_server_certificate=session.trust_server_certificate,
    )
    if error:
        raise HTTPException(status_code=400, detail=error)
    conn.close()

    session.database = database
    # Discard prior DB's cached results so /report and history stay consistent.
    session.last_analysis = None
    session.last_execution = None
    session.last_executions = []
    logger.info("Session %s… switched database to %s", x_session_token[:8], database)

    return {"server": session.server, "database": database}


@router.delete("/disconnect")
async def disconnect(x_session_token: str = Header(..., alias="X-Session-Token")):
    """Destroy the session and discard credentials."""
    deleted = store.delete(x_session_token)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found or already expired.")
    return {"message": "Disconnected successfully."}
