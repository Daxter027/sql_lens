"""
session.py
----------
In-memory session store. Maps a short-lived UUID token to connection
credentials. Credentials are NEVER written to disk, never logged, and
never returned to the client after the initial connect response.

The session token is the only thing the client holds; all sensitive data
stays exclusively on the server side for the session lifetime.
"""

import uuid
import time
import logging
import threading
from typing import Optional
from dataclasses import dataclass, field
from config import SESSION_TTL_SECONDS

logger = logging.getLogger(__name__)


@dataclass
class Session:
    token: str
    server: str
    database: str
    auth_type: str                  # "windows" | "sql"
    username: Optional[str]         # None for Windows auth
    password: Optional[str]         # None for Windows auth — never logged
    trust_server_certificate: bool
    created_at: float = field(default_factory=time.time)

    # Stored analysis results so the report endpoint can retrieve them
    # without re-running queries.
    last_analysis: Optional[dict] = None
    last_execution: Optional[dict] = None        # most recent single execution (back-compat)
    last_executions: list = field(default_factory=list)  # every execution this session

    def is_expired(self) -> bool:
        return (time.time() - self.created_at) > SESSION_TTL_SECONDS

    def __repr__(self) -> str:
        # Deliberately omit password from repr / logs
        return (
            f"Session(token={self.token[:8]}…, server={self.server}, "
            f"database={self.database}, auth={self.auth_type}, "
            f"user={self.username or 'windows-auth'})"
        )


class SessionStore:
    """Thread-safe in-memory session registry."""

    def __init__(self) -> None:
        self._store: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(
        self,
        server: str,
        database: str,
        auth_type: str,
        username: Optional[str],
        password: Optional[str],
        trust_server_certificate: bool = False,
    ) -> str:
        """Create a new session and return its token."""
        with self._lock:
            self._evict_expired()
            token = str(uuid.uuid4())
            self._store[token] = Session(
                token=token,
                server=server,
                database=database,
                auth_type=auth_type,
                username=username,
                password=password,
                trust_server_certificate=trust_server_certificate,
            )
            logger.info("Session created: %s", self._store[token])
            return token

    def get(self, token: str) -> Optional[Session]:
        with self._lock:
            session = self._store.get(token)
            if session is None:
                return None
            if session.is_expired():
                del self._store[token]
                logger.info("Session expired and evicted: %s…", token[:8])
                return None
            return session

    def delete(self, token: str) -> bool:
        with self._lock:
            if token in self._store:
                logger.info("Session deleted: %s…", token[:8])
                del self._store[token]
                return True
            return False

    def _evict_expired(self) -> None:
        """Must be called with self._lock held."""
        expired = [t for t, s in self._store.items() if s.is_expired()]
        for t in expired:
            del self._store[t]
            logger.debug("Evicted expired session: %s…", t[:8])


# ── Module-level singleton ───────────────────────────────────────────────────
store = SessionStore()
