"""
inappropriate_datatypes.py
---------------------------
Analysis module for Problem 24: Inappropriate Datatypes for Core Values.

Finds FLOAT/REAL columns that behave like identifiers/whole-number fields, where
floating-point storage is the wrong choice (rounding risk, join ambiguity,
storage overhead). For each candidate it samples actual values to check whether
any non-whole numbers exist, and runs a (non-exhaustive) dependency text search.

execute() is intentionally NOT implemented. Converting a column's datatype
(e.g. FLOAT → INT) is a one-way schema change that carries the SAME risk
category as the string-narrowing check (Issue 3): silent data loss or breakage
if any assumption is wrong. It is therefore never auto-executed — it requires
full-table verification (not a sample), confirmed-zero dependencies including
ones this check cannot see, and an application-layer compatibility review.
"""

from __future__ import annotations
import logging
from typing import Any
import pyodbc
from config import INAPPROPRIATE_DT_MAX_COLUMNS_TO_SCAN, INAPPROPRIATE_DT_SAMPLE_ROWS

logger = logging.getLogger(__name__)

ISSUE_ID   = "inappropriate_datatypes"
ISSUE_NAME = "Inappropriate Datatypes for Core Values"


def _q(identifier: str) -> str:
    """Bracket-quote a SQL identifier, escaping any embedded close-bracket."""
    return "[" + identifier.replace("]", "]]") + "]"


def _discover(cursor) -> list[dict]:
    """FLOAT/REAL columns on user tables, prioritised by table row count."""
    cursor.execute("""
        SELECT s.name AS schema_name, t.name AS table_name, c.name AS column_name,
               ty.name AS data_type, p.rows AS row_count
        FROM sys.tables t
        JOIN sys.columns c    ON c.object_id = t.object_id
        JOIN sys.types ty     ON ty.user_type_id = c.user_type_id
        JOIN sys.schemas s    ON s.schema_id = t.schema_id
        JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0,1)
        WHERE ty.name IN ('float','real')
          AND t.is_ms_shipped = 0
        ORDER BY p.rows DESC, t.name, c.name
    """)
    rows = cursor.fetchall()
    return [
        {"schema": r[0], "table": r[1], "column": r[2],
         "data_type": r[3], "row_count": int(r[4]) if r[4] is not None else 0}
        for r in rows
    ]


def _sample_table_for_decimals(cursor, schema: str, table: str, columns: list[str], row_count: int) -> dict:
    """
    Sample one table ONCE and report, per column, whether any non-whole-number
    value appears. A value is non-whole if it differs from its ROUND(...,0).
    Returns {column: {non_whole_count, ...}}; one scan covers every column.
    """
    src = f"{_q(schema)}.{_q(table)}"
    is_sampled = row_count > INAPPROPRIATE_DT_SAMPLE_ROWS
    sample_clause = f"TOP ({INAPPROPRIATE_DT_SAMPLE_ROWS})" if is_sampled else ""
    sample_cols = ", ".join(_q(c) for c in columns)
    exprs = ["COUNT_BIG(*)"]
    for c in columns:
        qc = _q(c)
        exprs.append(f"SUM(CONVERT(BIGINT, CASE WHEN {qc} IS NOT NULL AND {qc} <> ROUND({qc}, 0) "
                     f"THEN 1 ELSE 0 END))")
    try:
        cursor.execute(f"SELECT {', '.join(exprs)} "
                       f"FROM (SELECT {sample_clause} {sample_cols} FROM {src}) AS s")
        row = cursor.fetchone()
        sampled_rows = int(row[0]) if row and row[0] is not None else 0
        out = {}
        for idx, c in enumerate(columns):
            nw = row[1 + idx]
            out[c] = {"sampled": is_sampled, "sample_rows": sampled_rows,
                      "non_whole_count": int(nw) if nw is not None else 0, "ok": True}
        return out
    except pyodbc.Error as exc:
        logger.warning("inappropriate_datatypes: sample failed for %s: %s", src, exc)
        return {c: {"sampled": is_sampled, "sample_rows": 0, "non_whole_count": None, "ok": False}
                for c in columns}


def _dependency_count(cursor, column: str) -> int:
    """Non-exhaustive text search of module definitions for the column name."""
    try:
        cursor.execute("SELECT COUNT(*) FROM sys.sql_modules WHERE definition LIKE ?", f"%{column}%")
        return int(cursor.fetchone()[0])
    except pyodbc.Error:
        return -1


def analyze(conn: pyodbc.Connection) -> dict[str, Any]:
    cursor = conn.cursor()
    all_cols = _discover(cursor)
    total_float_cols = len(all_cols)
    targets = all_cols[:INAPPROPRIATE_DT_MAX_COLUMNS_TO_SCAN]

    # Group by table so each table is sampled ONCE for all its FLOAT/REAL columns.
    by_table: dict[tuple, dict] = {}
    for col in targets:
        key = (col["schema"], col["table"])
        entry = by_table.setdefault(key, {"row_count": col["row_count"], "cols": []})
        entry["cols"].append(col)

    findings = []
    for (schema, table), info in by_table.items():
        col_names = [c["column"] for c in info["cols"]]
        sampled = _sample_table_for_decimals(cursor, schema, table, col_names, info["row_count"])
        for col in info["cols"]:
            s = sampled.get(col["column"], {"ok": False, "non_whole_count": None,
                                            "sample_rows": 0, "sampled": False})
            dep = _dependency_count(cursor, col["column"])
            # "Looks like an identifier" = sampled cleanly with zero decimals seen.
            looks_identifier = bool(s["ok"] and s["non_whole_count"] == 0 and s["sample_rows"] > 0)
            findings.append({
                "schema":          schema,
                "table":           table,
                "column":          col["column"],
                "data_type":       col["data_type"],
                "row_count":       col["row_count"],
                "sampled":         s["sampled"],
                "sample_rows":     s["sample_rows"],
                "non_whole_count": s["non_whole_count"],
                "looks_like_identifier": looks_identifier,
                "dependency_count": dep,
            })

    identifier_like = [f for f in findings if f["looks_like_identifier"]]
    note = (
        f"Found {total_float_cols} FLOAT/REAL column(s); sampled the top "
        f"{len(targets)} by row count at {INAPPROPRIATE_DT_SAMPLE_ROWS:,} rows each. "
        "Sampling can miss rare decimal values — a conversion needs a FULL-table check. "
        "The dependency count is a non-exhaustive text search (cannot see dynamic SQL "
        "or application-layer references)."
    )

    if total_float_cols == 0:
        return {
            "issue_id":   ISSUE_ID,
            "issue_name": ISSUE_NAME,
            "severity":   "Low",
            "affected_objects": [],
            "current_metrics": {"float_columns": 0, "identifier_like": 0},
            "recommended_action": "No FLOAT/REAL columns found.",
            "estimated_impact": "N/A",
            "executable":       False,
            "eligible_for_fix": False,
            "blocking_reason":  "No FLOAT/REAL columns present.",
            "analysis_note":    note,
        }

    severity = "Medium" if len(identifier_like) > 0 else "Low"
    return {
        "issue_id":   ISSUE_ID,
        "issue_name": ISSUE_NAME,
        "severity":   severity,
        "affected_objects": findings,
        "current_metrics": {
            "float_columns":   total_float_cols,
            "identifier_like": len(identifier_like),
        },
        "recommended_action": (
            f"{total_float_cols} FLOAT/REAL column(s) found; {len(identifier_like)} of the sampled "
            "columns held only whole numbers and behave like identifiers. Converting such a column "
            "to INT/BIGINT improves schema clarity and removes rounding risk, but is NEVER "
            "auto-executed: it requires (1) full-table verification that zero decimal values exist "
            "anywhere, (2) confirmed-zero dependent objects including ones this check cannot see, and "
            "(3) an application-layer compatibility review. Same risk category as the string-"
            "narrowing check — silent data loss or breakage if any assumption is wrong."
        ),
        "estimated_impact": "Reduced storage and rounding/precision risk on identifier fields (after a fully validated, manual conversion).",
        "executable":       False,
        "eligible_for_fix": False,
        "blocking_reason":  None,
        "analysis_note":    note,
    }


def execute(*args, **kwargs):
    raise NotImplementedError(
        "Datatype conversion requires full validation and application-level review "
        "outside this tool — analysis only in this version."
    )
