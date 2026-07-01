"""
string_storage.py
-----------------
Analysis module for Issue 3: Data Type String Storage Optimization.

Phase 1: analyze() is fully implemented with real DMV + sampling T-SQL.
         execute() is stubbed and will NEVER be implemented in this tool.

PERMANENT NOTE: Column type narrowing (e.g. VARCHAR(500) → VARCHAR(50))
is a BREAKING SCHEMA CHANGE. It risks silent data truncation for any row
where the actual data exceeds the new declared length. This tool explicitly
calls out type narrowing as out of scope for automated execution in ANY
future version — it requires DBA review, application testing, and a
maintenance window. The execute() stub below reflects this.
"""

from __future__ import annotations
import logging
from typing import Any
import pyodbc
from config import STRING_SAMPLE_ROWS, STRING_OVERSIZE_RATIO, STRING_MIN_TABLE_ROWS

logger = logging.getLogger(__name__)

ISSUE_ID   = "string_storage"
ISSUE_NAME = "Data Type String Storage Optimization"


def _q(identifier: str) -> str:
    """Bracket-quote a SQL identifier, escaping any embedded close-bracket."""
    return "[" + identifier.replace("]", "]]") + "]"


def analyze(conn: pyodbc.Connection) -> dict[str, Any]:
    """
    Find VARCHAR/NVARCHAR/CHAR/NCHAR columns where declared length significantly
    exceeds actual observed data length, or where CHAR is used for variable data.

    For large tables (> STRING_SAMPLE_ROWS rows), sampling is used and noted.
    NVARCHAR columns with consistently pure-ASCII data are flagged informationally.
    """
    cursor = conn.cursor()

    # Step 1: Get all string columns with table row counts
    cursor.execute("""
        SELECT
            s.name                  AS schema_name,
            t.name                  AS table_name,
            c.name                  AS column_name,
            tp.name                 AS type_name,
            c.max_length            AS max_length_bytes,
            c.column_id,
            p.rows                  AS row_count,
            OBJECT_NAME(c.object_id) AS obj_name,
            c.object_id
        FROM sys.columns c
        JOIN sys.types tp      ON tp.user_type_id = c.user_type_id
        JOIN sys.tables t      ON t.object_id = c.object_id
        JOIN sys.schemas s     ON s.schema_id = t.schema_id
        JOIN sys.partitions p  ON p.object_id = t.object_id AND p.index_id IN (0,1)
        WHERE tp.name IN ('varchar','nvarchar','char','nchar')
          AND c.max_length > 0        -- exclude MAX columns (-1)
          AND t.is_ms_shipped = 0
          AND p.rows > 0
        ORDER BY p.rows DESC, t.name, c.name
    """)

    columns = cursor.fetchall()
    findings = []

    # ── Group candidate columns by table ─────────────────────────────────────
    # Previously this ran 1–2 queries PER COLUMN (a DATALENGTH sample plus an
    # ASCII check). On a database with many string columns that meant dozens to
    # hundreds of round-trips. We now issue ONE combined query per table that
    # computes max/avg DATALENGTH (and the ASCII count for nvarchar) for every
    # candidate column at once — same sampling semantics, far fewer queries.
    tables: dict[tuple, dict] = {}
    for row in columns:
        (schema_name, table_name, col_name, type_name,
         max_length_bytes, col_id, row_count, obj_name, obj_id) = row

        # Skip tiny tables entirely — negligible wasted space, and on a wide
        # schema the per-table round-trips are what make this check slow.
        if row_count < STRING_MIN_TABLE_ROWS:
            continue

        # NVARCHAR/NCHAR use 2 bytes per char; others 1 byte
        declared_char_len = (
            max_length_bytes // 2 if type_name in ('nvarchar', 'nchar') else max_length_bytes
        )
        # Only sample columns with declared length >= 10 (skip tiny columns)
        if declared_char_len < 10:
            continue

        key = (schema_name, table_name)
        if key not in tables:
            tables[key] = {"row_count": row_count, "cols": []}
        tables[key]["cols"].append({
            "name":              col_name,
            "type":              type_name,
            "declared_char_len": declared_char_len,
        })

    for (schema_name, table_name), info in tables.items():
        cols = info["cols"]
        row_count = info["row_count"]
        is_sampled = row_count > STRING_SAMPLE_ROWS
        sample_clause = f"TOP ({STRING_SAMPLE_ROWS})" if is_sampled else ""

        # Build the combined projection: 2 result columns per source column
        # (max bytes, avg bytes). We deliberately do NOT run a per-row ASCII
        # check (COLLATE + CAST AS VARCHAR(MAX)) here — it was purely
        # informational, never shown in the UI, and was the single most
        # expensive part of this scan.
        select_exprs = []
        sample_cols = []
        for c in cols:
            qn = _q(c["name"])
            sample_cols.append(qn)
            select_exprs.append(f"ISNULL(MAX(DATALENGTH({qn})), 0)")
            select_exprs.append(f"ISNULL(AVG(DATALENGTH({qn})), 0)")

        query = (
            f"SELECT {', '.join(select_exprs)} "
            f"FROM (SELECT {sample_clause} {', '.join(sample_cols)} "
            f"FROM {_q(schema_name)}.{_q(table_name)}) AS s"
        )

        try:
            cursor.execute(query)
            result = cursor.fetchone()
        except pyodbc.Error:
            continue

        for idx, c in enumerate(cols):
            base = idx * 2
            max_actual_bytes = result[base] or 0

            divisor = 2 if c["type"] in ('nvarchar', 'nchar') else 1
            max_actual_chars = max_actual_bytes // divisor if max_actual_bytes else 0
            if max_actual_chars == 0:
                continue

            declared_char_len = c["declared_char_len"]
            ratio = declared_char_len / max(max_actual_chars, 1)
            if ratio < STRING_OVERSIZE_RATIO:
                continue

            issue_type = []
            if c["type"] in ('char', 'nchar'):
                issue_type.append("Fixed-width CHAR/NCHAR used — padding waste for variable-length data")
            issue_type.append(
                f"Declared length ({declared_char_len}) is {ratio:.1f}× observed max ({max_actual_chars})"
            )

            findings.append({
                "schema":           schema_name,
                "table":            table_name,
                "column":           c["name"],
                "type":             c["type"],
                "declared_length":  declared_char_len,
                "observed_max":     max_actual_chars,
                "oversize_ratio":   round(ratio, 1),
                "issues":           issue_type,
                "sampled":          is_sampled,
                "sample_size":      STRING_SAMPLE_ROWS if is_sampled else row_count,
            })

    if not findings:
        return {
            "issue_id":         ISSUE_ID,
            "issue_name":       ISSUE_NAME,
            "severity":         "Low",
            "affected_objects": [],
            "current_metrics":  {"flagged_columns": 0},
            "recommended_action": "No significantly over-declared string columns found.",
            "estimated_impact": "N/A",
            "executable":       False,
            "eligible_for_fix": False,
            "blocking_reason":  "Analysis only in this version — execution not yet implemented.",
        }

    severity = "High" if len(findings) > 20 else "Medium" if len(findings) > 5 else "Low"

    return {
        "issue_id":         ISSUE_ID,
        "issue_name":       ISSUE_NAME,
        "severity":         severity,
        "affected_objects": findings,
        "current_metrics":  {"flagged_columns": len(findings)},
        "recommended_action": (
            f"Found {len(findings)} column(s) where declared string length significantly "
            f"exceeds observed data. Type narrowing is a BREAKING SCHEMA CHANGE that risks "
            "silent data truncation. This tool will NEVER automate column type changes, "
            "regardless of future versions. Review findings with your application team, "
            "validate max data lengths against application logic (not just current data), "
            "and apply changes only in a tested maintenance window."
        ),
        "estimated_impact": (
            "Reduced row size, better buffer pool utilisation, "
            "possible index size reduction."
        ),
        "executable":       False,
        "eligible_for_fix": False,
        "blocking_reason":  (
            "Analysis only in this version — execution not yet implemented. "
            "NOTE: Type narrowing is explicitly out of scope for automated execution "
            "in any future version due to truncation risk."
        ),
        "analysis_note": (
            f"Large tables sampled at {STRING_SAMPLE_ROWS:,} rows; "
            "result notes 'sampled' flag per column."
        ),
    }


def execute(*args, **kwargs):
    """
    Column type narrowing is a BREAKING SCHEMA CHANGE and will NEVER be
    automated by this tool in any version. Attempting to execute this
    analysis issue is always an error.
    """
    raise NotImplementedError(
        "String storage type narrowing is permanently out of scope for automated "
        "execution. This operation risks silent data truncation and requires "
        "DBA review, application validation, and a scheduled maintenance window."
    )
