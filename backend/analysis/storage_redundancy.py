"""
storage_redundancy.py
---------------------
"Storage & Redundancy Analysis" — finds the top ~20% largest tables by storage
and has the Anthropic Claude API produce a short, fixed-template Markdown report.

SINGLE PUBLIC ENTRY POINT: run_storage_redundancy_analysis(conn).
The SQL query and the Claude API call happen sequentially inside that one
function; only the final combined dict is returned. The private _-helpers below
exist for readability only — they are not separate public entry points.

This is the codebase's only non-deterministic / network feature, so it is NOT
one of the parallel /analyze checks (a network round-trip would stall the batch).
It is exposed via its own on-demand endpoint.

PRIVACY: this sends table NAMES + row counts + sizes (never row contents) to
Anthropic's cloud API. The API key is read from ANTHROPIC_API_KEY and is never
logged or returned to the client.
"""

from __future__ import annotations
import json
import logging
import math
import socket
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

import pyodbc
from config import (
    ANTHROPIC_API_KEY, ANTHROPIC_BASE_URL, ANTHROPIC_MODEL, ANTHROPIC_VERSION,
    ANTHROPIC_TIMEOUT_SECONDS, ANTHROPIC_MAX_TOKENS, ANTHROPIC_TEMPERATURE,
    STORAGE_REDUNDANCY_ROW_CAP,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Pure helper (unit-tested without a DB)
# ─────────────────────────────────────────────────────────────────────────────

def top_n(table_count: int) -> int:
    """
    Top 20% by storage, rounded UP (CEILING — at least 20%, never less).
    Floor of 1 for tiny DBs, cap of 2000 to avoid runaway queries. 0 → 0
    (caller short-circuits before this when the DB has no user tables).
    """
    if table_count <= 0:
        return 0
    return max(1, min(2000, math.ceil(table_count * 0.20)))


# Partition-safe size query: ONE row per table (GROUP BY object_id), using
# sys.dm_db_partition_stats (already aggregated per partition/index — no
# allocation-unit row multiplication, and index_id IN (0,1) avoids counting
# every non-clustered index as a separate table). Mirrors the DMV the rest of
# the codebase uses (ghost_pages / index_fragmentation).
_TOP_TABLES_SQL = """
    SELECT TOP (?)
        t.name AS TableName,
        s.name AS SchemaName,
        SUM(ps.row_count)                                                          AS [RowCount],
        ROUND(SUM(ps.reserved_page_count) * 8 / 1024.0, 2)                         AS TotalSpaceMB,
        ROUND(SUM(ps.used_page_count)     * 8 / 1024.0, 2)                         AS UsedSpaceMB,
        ROUND((SUM(ps.reserved_page_count) - SUM(ps.used_page_count)) * 8 / 1024.0, 2) AS UnusedSpaceMB
    FROM sys.tables t
    JOIN sys.schemas s                ON s.schema_id = t.schema_id
    JOIN sys.dm_db_partition_stats ps ON ps.object_id = t.object_id
    WHERE t.is_ms_shipped = 0
      AND ps.index_id IN (0, 1)
    GROUP BY t.object_id, t.name, s.name
    ORDER BY TotalSpaceMB DESC
"""


# ─────────────────────────────────────────────────────────────────────────────
# Anthropic Claude client (stdlib urllib only — no SDK / new dependency)
# ─────────────────────────────────────────────────────────────────────────────

class ApiUnreachable(Exception): ...     # network: cannot reach api.anthropic.com
class ApiAuthError(Exception): ...       # missing / invalid API key (401/403)
class ModelNotFound(Exception): ...      # unknown model name (404)
class ApiRateLimited(Exception): ...     # quota / rate limit (429)
class ApiTimeout(Exception): ...         # request timed out
class ApiError(Exception): ...           # any other API failure


def _call_claude(prompt: str, *, model: str, base_url: str, api_key: str,
                 timeout: int, max_tokens: int, temperature: float) -> str:
    """
    POST /v1/messages (Anthropic Messages API); return the concatenated text.
    The api_key is sent only in the x-api-key header — never logged.
    """
    if not api_key:
        raise ApiAuthError(
            "No Anthropic API key is configured. Set the ANTHROPIC_API_KEY "
            "environment variable (then restart the backend) and try again.")
    url = base_url.rstrip("/") + "/v1/messages"
    payload = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST", headers={
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "ignore")
        except Exception:
            pass
        if exc.code in (401, 403):
            raise ApiAuthError(
                f"The Anthropic API rejected the request (HTTP {exc.code}). "
                "Check that ANTHROPIC_API_KEY is valid and active.")
        if exc.code == 404 or "not_found_error" in body.lower():
            raise ModelNotFound(
                f"Model '{model}' was not found by the Anthropic API. "
                "Check the model name (e.g. claude-sonnet-4-6).")
        if exc.code == 429 or "rate_limit" in body.lower():
            raise ApiRateLimited(
                "The Anthropic API rate limit or quota was reached (HTTP 429). "
                "Wait a moment and try again, or check your plan's limits.")
        raise ApiError(f"The Anthropic API returned HTTP {exc.code}.")
    except socket.timeout:
        raise ApiTimeout("timeout")
    except urllib.error.URLError as exc:
        reason = str(getattr(exc, "reason", exc)).lower()
        if "timed out" in reason:
            raise ApiTimeout("timeout")
        raise ApiUnreachable(
            f"Could not reach the Anthropic API at {base_url}. "
            "Check this machine's internet connection.")
    # Messages API returns content as a list of blocks; join the text ones.
    blocks = (data or {}).get("content") or []
    text = "".join(
        b.get("text", "") for b in blocks
        if isinstance(b, dict) and b.get("type") == "text")
    if not text.strip():
        raise ApiError("The Anthropic API returned an empty response.")
    text = text.strip()
    # If the model was cut off by the output-token cap, say so explicitly rather
    # than returning a silently-truncated report.
    if (data or {}).get("stop_reason") == "max_tokens":
        text += (
            "\n\n> ⚠ **Report truncated** — the model hit the output-token limit "
            f"(`ANTHROPIC_MAX_TOKENS` = {max_tokens}). Raise it to get the full report.")
    return text


# ─────────────────────────────────────────────────────────────────────────────
# Formatting + prompt (private)
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_row(r: dict) -> dict:
    return {
        "TableName":     r.get("TableName"),
        "SchemaName":    r.get("SchemaName"),
        "RowCount":      int(r.get("RowCount") or 0),
        "TotalSpaceMB":  float(r.get("TotalSpaceMB") or 0),
        "UsedSpaceMB":   float(r.get("UsedSpaceMB") or 0),
        "UnusedSpaceMB": float(r.get("UnusedSpaceMB") or 0),
    }


def _format_rows(rows: list[dict], cap: int) -> tuple[str, bool]:
    """Tab-separated, token-efficient view; truncated to `cap` rows for the model."""
    was_truncated = len(rows) > cap
    subset = rows[:cap]
    header = "TableName\tSchemaName\tRowCount\tTotalSpaceMB\tUsedSpaceMB\tUnusedSpaceMB"
    lines = [header]
    for r in subset:
        lines.append(
            f"{r['TableName']}\t{r['SchemaName']}\t{r['RowCount']}\t"
            f"{r['TotalSpaceMB']}\t{r['UsedSpaceMB']}\t{r['UnusedSpaceMB']}")
    return "\n".join(lines), was_truncated


def _build_prompt(formatted_table: str, was_truncated: bool,
                  analyzed_count: int, total_count: int) -> str:
    trunc = ("\nNOTE: This is a TRUNCATED view (only the largest tables by size). "
             "Mention in your output that the analysis is based on a truncated subset.\n"
             if was_truncated else "")
    return f"""You are a SQL Server storage analyst. Analyze the table data and fill in the EXACT Markdown template below. Be concise and factual, and use ONLY the data given.

The data is tab-separated, one table per line, columns:
TableName, SchemaName, RowCount, TotalSpaceMB, UsedSpaceMB, UnusedSpaceMB
({analyzed_count} tables shown, out of {total_count} user tables total.)
{trunc}
DATA:
{formatted_table}

Fill in EVERY section. Do not add, remove, or rename sections.

## Largest Tables
List the 3-5 tables with the highest TotalSpaceMB, each with its size in MB.

## Naming Pattern Matches
Scan TableName values for these suffixes/patterns and list matches: _Old, _New, _Temp, _Backup, _BK, _Deleted, and names ending in a 6-8 digit number that looks like a date.
Example: if you see both 'Orders' and 'Orders_Old', report that pair.
Example: 'Invoices_Backup' is a match.
Example: 'Sales_20231130' ends in a date-like number — a match.
If none, write "No naming patterns matched."

## Similar Row Counts
List any two or more tables whose RowCount values are within 2% of each other, by name, labeled "similar row counts — possible duplicate or snapshot." If none, write "No similar row counts found."

## High Fragmentation
List tables where UnusedSpaceMB is more than 50% of TotalSpaceMB, labeled "high fragmentation — review for rebuild." If none, write "No high-fragmentation tables found."

## Recommended Next Steps
Give 3-5 concrete next steps, each naming a specific table.

RULES:
- NEVER claim a table is "unused". Only say it "shows a naming/size/row-count pattern worth reviewing."
- Base everything strictly on the data above. Do not invent tables."""


def _result(*, status, total=0, analyzed=0, was_truncated=False, table_data=None,
            analysis_markdown=None, model_used=None, error=None, error_kind=None, message="") -> dict:
    return {
        "status": status,
        "total_user_table_count": total,
        "analyzed_table_count": analyzed,
        "analyzed_percentage": round(analyzed / total * 100, 1) if total else 0,
        "was_truncated": was_truncated,
        "table_data": table_data or [],
        "analysis_markdown": analysis_markdown,
        "model_used": model_used,
        "error": error,
        "error_kind": error_kind,
        "message": message,
    }


# ─────────────────────────────────────────────────────────────────────────────
# THE single public entry point
# ─────────────────────────────────────────────────────────────────────────────

def run_storage_redundancy_analysis(
    conn: pyodbc.Connection,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    row_cap: Optional[int] = None,
    ai_call: Optional[Callable[..., str]] = None,   # injectable for tests
) -> dict[str, Any]:
    """
    One function, executed top to bottom:
      1. count user tables → 2. top-20% storage query → 3. format (capped) →
      4. Claude API call (fixed checklist + template) → 5. combined result.
    The model is NEVER called with empty/partial data: any DB failure returns
    before step 4.
    """
    model = model or ANTHROPIC_MODEL
    base_url = base_url or ANTHROPIC_BASE_URL
    row_cap = row_cap or STORAGE_REDUNDANCY_ROW_CAP
    ai_call = ai_call or _call_claude

    # ── STEP 1: total user table count ───────────────────────────────────────
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM sys.tables t WHERE t.is_ms_shipped = 0")
        total = int(cursor.fetchone()[0])
    except pyodbc.Error:
        logger.error("storage_redundancy: table-count query failed", exc_info=True)
        return _result(status="error", error_kind="db_error",
                       error="Failed to query the database for table counts.",
                       message="Database query failed.")

    if total == 0:
        return _result(status="empty", total=0,
                       message="No user tables found in this database.")

    requested = top_n(total)

    # ── STEP 2: top-20% storage query (one accurate row per table) ───────────
    try:
        cursor.execute(_TOP_TABLES_SQL, requested)
        cols = [d[0] for d in cursor.description]
        table_data = [_normalize_row(dict(zip(cols, r))) for r in cursor.fetchall()]
    except pyodbc.Error:
        logger.error("storage_redundancy: storage query failed", exc_info=True)
        # Fail BEFORE the model — never analyze partial/empty data.
        return _result(status="error", total=total, analyzed=requested,
                       error_kind="db_error",
                       error="Failed to query table storage metrics.",
                       message="Database query failed.")

    # ── STEP 3: token-efficient formatting (capped to keep the prompt bounded) ─
    formatted, was_truncated = _format_rows(table_data, row_cap)

    # ── STEP 4: Claude API call ──────────────────────────────────────────────
    prompt = _build_prompt(formatted, was_truncated, min(len(table_data), row_cap), total)
    try:
        analysis_md = ai_call(
            prompt, model=model, base_url=base_url, api_key=ANTHROPIC_API_KEY,
            timeout=ANTHROPIC_TIMEOUT_SECONDS,
            max_tokens=ANTHROPIC_MAX_TOKENS, temperature=ANTHROPIC_TEMPERATURE)
    except ApiAuthError as exc:
        return _result(status="error", total=total, analyzed=requested, table_data=table_data,
                       error_kind="auth_error", error=str(exc), message=str(exc))
    except ApiUnreachable as exc:
        return _result(status="error", total=total, analyzed=requested, table_data=table_data,
                       error_kind="api_unreachable", error=str(exc), message=str(exc))
    except ModelNotFound as exc:
        return _result(status="error", total=total, analyzed=requested, table_data=table_data,
                       error_kind="model_not_found", error=str(exc), message=str(exc))
    except ApiRateLimited as exc:
        return _result(status="error", total=total, analyzed=requested, table_data=table_data,
                       error_kind="rate_limited", error=str(exc), message=str(exc))
    except ApiTimeout:
        msg = ("The Anthropic API request timed out. Check the connection and try again, "
               "or lower the row cap / max tokens.")
        return _result(status="error", total=total, analyzed=requested, table_data=table_data,
                       error_kind="timeout", error=msg, message=msg)
    except Exception:
        logger.error("storage_redundancy: Claude API call failed", exc_info=True)
        msg = "The Claude API call failed. Check the API key, model name and connection."
        return _result(status="error", total=total, analyzed=requested, table_data=table_data,
                       error_kind="api_error", error=msg, message=msg)

    # ── STEP 5: combined result ──────────────────────────────────────────────
    return _result(
        status="ok", total=total, analyzed=requested, was_truncated=was_truncated,
        table_data=table_data, analysis_markdown=analysis_md, model_used=model,
        message="ok")
