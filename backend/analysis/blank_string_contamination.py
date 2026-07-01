"""
blank_string_contamination.py
------------------------------
Analysis + execution module for Problem 14: Blank String Bypass Contamination.

Finds empty strings ('') and whitespace-only values in text columns that slip
past NULL validation and leave records with no meaningful business data.

execute() converts those already-meaningless values to NULL. This is safe to
automate because it only ever replaces an empty/whitespace-only value with NULL
— no value a person would consider real data is touched, and the change is
trivially auditable.

SAFETY NOTE ON NULLABILITY (deliberate refinement of the source playbook):
  The source script runs `UPDATE ... SET col = NULL` against NOT NULL columns.
  That statement *fails* on a NOT NULL column (NULL isn't allowed). Converting a
  blank to NULL is only valid when the column permits NULL. So:
    - The fix is offered ONLY for NULLABLE text columns (eligible_for_fix).
    - NOT NULL columns that contain blanks are still reported as findings, but
      flagged not-eligible — clearing them would require first making the column
      nullable (a schema change) which is out of scope for an automated fix.
  The "ADD CONSTRAINT CK_..._NotBlank" preventive step from the source is also
  deliberately NOT bundled here — that is a separate schema change.
"""

from __future__ import annotations
import logging
from typing import Any, Optional
import pyodbc
from config import BLANK_STRING_MAX_COLUMNS_TO_SCAN

logger = logging.getLogger(__name__)

ISSUE_ID   = "blank_string_contamination"
ISSUE_NAME = "Blank String Bypass Contamination"


def _q(identifier: str) -> str:
    """Bracket-quote a SQL identifier, escaping any embedded close-bracket."""
    return "[" + identifier.replace("]", "]]") + "]"


def _discover_columns(cursor) -> list[dict]:
    """
    Populated text columns worth checking, prioritised by row count. We include
    BOTH nullable and NOT NULL columns: NOT NULL ones are the classic "blank
    bypass" target (reported), but only nullable ones can be safely fixed.
    """
    cursor.execute("""
        SELECT s.name AS schema_name, t.name AS table_name, c.name AS column_name,
               ty.name AS data_type, c.is_nullable, p.rows AS row_count
        FROM sys.tables t
        JOIN sys.columns c    ON c.object_id = t.object_id
        JOIN sys.types ty     ON ty.user_type_id = c.user_type_id
        JOIN sys.schemas s    ON s.schema_id = t.schema_id
        JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0,1)
        WHERE ty.name IN ('varchar','nvarchar','char','nchar')
          AND t.is_ms_shipped = 0
          AND p.rows > 0
        ORDER BY p.rows DESC, t.name, c.name
    """)
    rows = cursor.fetchall()
    return [
        {"schema": r[0], "table": r[1], "column": r[2],
         "data_type": r[3], "is_nullable": bool(r[4]), "row_count": int(r[5])}
        for r in rows[:BLANK_STRING_MAX_COLUMNS_TO_SCAN]
    ]


def _count_blanks(cursor, schema: str, table: str, column: str) -> Optional[tuple[int, int, int]]:
    """Return (total_rows, blank_strings, blank_or_spaces) for one column."""
    qc, src = _q(column), f"{_q(schema)}.{_q(table)}"
    try:
        cursor.execute(f"""
            SELECT COUNT_BIG(*),
                   SUM(CONVERT(BIGINT, CASE WHEN {qc} = '' THEN 1 ELSE 0 END)),
                   SUM(CONVERT(BIGINT, CASE WHEN LEN(LTRIM(RTRIM({qc}))) = 0 THEN 1 ELSE 0 END))
            FROM {src}
        """)
        row = cursor.fetchone()
        return (int(row[0]), int(row[1] or 0), int(row[2] or 0))
    except pyodbc.Error as exc:
        logger.warning("blank_string: count failed for [%s].[%s].[%s]: %s", schema, table, column, exc)
        return None


def analyze(conn: pyodbc.Connection) -> dict[str, Any]:
    cursor = conn.cursor()
    targets = _discover_columns(cursor)

    # Group candidate columns by table so each table is scanned ONCE (all its
    # columns' blank counts computed in a single pass) instead of once-per-column.
    by_table: dict[tuple, dict] = {}
    for col in targets:
        key = (col["schema"], col["table"])
        by_table.setdefault(key, {"cols": []})["cols"].append(col)

    findings = []
    for (schema, table), info in by_table.items():
        cols = info["cols"]
        src = f"{_q(schema)}.{_q(table)}"
        exprs = ["COUNT_BIG(*)"]
        for c in cols:
            qc = _q(c["column"])
            exprs.append(f"SUM(CONVERT(BIGINT, CASE WHEN {qc} = '' THEN 1 ELSE 0 END))")
            exprs.append(f"SUM(CONVERT(BIGINT, CASE WHEN LEN(LTRIM(RTRIM({qc}))) = 0 THEN 1 ELSE 0 END))")
        try:
            cursor.execute(f"SELECT {', '.join(exprs)} FROM {src}")
            row = cursor.fetchone()
        except pyodbc.Error as exc:
            logger.warning("blank_string: scan failed for %s: %s", src, exc)
            continue

        total = int(row[0]) if row[0] is not None else 0
        for idx, c in enumerate(cols):
            blanks = int(row[1 + idx * 2] or 0)
            blank_or_spaces = int(row[2 + idx * 2] or 0)
            if blank_or_spaces <= 0:
                continue  # "No contamination" is a valid outcome — not flagged
            findings.append({
                "schema":          schema,
                "table":           table,
                "column":          c["column"],
                "data_type":       c["data_type"],
                "is_nullable":     c["is_nullable"],
                "total_rows":      total,
                "blank_strings":   blanks,
                "blank_or_spaces": blank_or_spaces,
                # Only nullable columns can be safely converted to NULL.
                "eligible":        c["is_nullable"],
            })

    eligible = [f for f in findings if f["eligible"]]
    not_nullable_hits = [f for f in findings if not f["eligible"]]
    total_blanks = sum(f["blank_or_spaces"] for f in findings)
    scan_note = (
        f"Scanned {len(targets)} text column(s) (cap {BLANK_STRING_MAX_COLUMNS_TO_SCAN}, "
        "prioritised by row count). 'No contamination' is a valid finding."
    )

    if not findings:
        return {
            "issue_id":   ISSUE_ID,
            "issue_name": ISSUE_NAME,
            "severity":   "Low",
            "affected_objects": [],
            "current_metrics": {"flagged_columns": 0, "total_blank_values": 0, "fixable_columns": 0},
            "recommended_action": "No blank-string contamination detected in the scanned text columns.",
            "estimated_impact": "N/A",
            "executable":       True,
            "eligible_for_fix": False,
            "blocking_reason":  "No blank/whitespace-only values found.",
            "analysis_note":    scan_note,
        }

    severity = "High" if total_blanks > 10_000 else "Medium" if total_blanks > 100 else "Low"
    action = (
        f"Found {total_blanks:,} blank/whitespace-only value(s) across {len(findings)} column(s). "
        f"{len(eligible)} are in nullable columns and can be converted to NULL automatically "
        "(safe — only meaningless empty values are touched)."
    )
    if not_nullable_hits:
        action += (
            f" {len(not_nullable_hits)} are in NOT NULL columns and are NOT auto-fixable: "
            "clearing them needs the column made nullable first (a schema change, out of scope)."
        )

    return {
        "issue_id":   ISSUE_ID,
        "issue_name": ISSUE_NAME,
        "severity":   severity,
        "affected_objects": findings,
        "current_metrics": {
            "flagged_columns":    len(findings),
            "total_blank_values": total_blanks,
            "fixable_columns":    len(eligible),
        },
        "recommended_action": action,
        "estimated_impact": "Cleaner mandatory fields; blank values no longer masquerade as real data.",
        "executable":       True,
        "eligible_for_fix": len(eligible) > 0,
        "blocking_reason":  None if eligible else "Blanks exist only in NOT NULL columns — schema change required first.",
        "analysis_note":    scan_note,
    }


def _process_single(conn: pyodbc.Connection, schema: str, table: str, column: str) -> dict:
    """Convert blank/whitespace-only values in one nullable column to NULL."""
    cursor = conn.cursor()
    base = {"schema": schema, "table": table, "column": column,
            "command_executed": None, "before_metrics": None, "after_metrics": None}

    # ── Confirm column exists and is nullable ────────────────────────────────
    cursor.execute("""
        SELECT c.is_nullable
        FROM sys.columns c
        JOIN sys.tables t  ON t.object_id = c.object_id
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        WHERE s.name = ? AND t.name = ? AND c.name = ?
    """, schema, table, column)
    row = cursor.fetchone()
    if not row:
        return {**base, "status": "skipped", "message": f"Column [{column}] not found."}
    if not bool(row[0]):
        return {**base, "status": "skipped",
                "message": f"Column [{column}] is NOT NULL — cannot convert blanks to NULL "
                           "without first making the column nullable (out of scope)."}

    # ── Permission check ─────────────────────────────────────────────────────
    try:
        cursor.execute("SELECT HAS_PERMS_BY_NAME(?, 'OBJECT', 'UPDATE'), IS_SRVROLEMEMBER('sysadmin')",
                       f"{schema}.{table}")
        has_upd, is_sa = cursor.fetchone()
        if not has_upd and not is_sa:
            return {**base, "status": "skipped", "message": "Current login lacks UPDATE permission on the table."}
    except pyodbc.Error:
        pass

    # ── Pre-check: re-count fresh (do NOT trust the analyze snapshot) ────────
    before = _count_blanks(cursor, schema, table, column)
    if before is None:
        return {**base, "status": "failed", "message": "Could not read current blank count."}
    if before[2] == 0:
        return {**base, "status": "skipped",
                "message": "Precondition no longer met — no blank values remain.",
                "before_metrics": {"blank_or_spaces": 0}}

    audit_cmd = (f"UPDATE [{schema}].[{table}] SET [{column}] = NULL "
                 f"WHERE LEN(LTRIM(RTRIM([{column}]))) = 0")

    # ── Execute (QUOTENAME-quoted identifiers via sp_executesql) ─────────────
    # Placeholders in statement order: schema, table, column, column.
    sql = (
        "DECLARE @sql NVARCHAR(MAX) = N'UPDATE ' + QUOTENAME(?) + N'.' + QUOTENAME(?) + "
        "N' SET ' + QUOTENAME(?) + N' = NULL WHERE LEN(LTRIM(RTRIM(' + QUOTENAME(?) + N'))) = 0'; "
        "EXEC sp_executesql @sql;"
    )
    try:
        conn.autocommit = True
        cursor.execute(sql, schema, table, column, column)
        conn.autocommit = False
    except pyodbc.Error:
        conn.autocommit = False
        logger.error("blank_string UPDATE failed (details not forwarded to client)")
        return {**base, "status": "failed", "command_executed": audit_cmd,
                "message": "Failed to convert blank values to NULL.",
                "before_metrics": {"total_rows": before[0], "blank_or_spaces": before[2]}}

    # ── Post-verify ──────────────────────────────────────────────────────────
    after = _count_blanks(cursor, schema, table, column)
    after_blanks = after[2] if after else None
    cleared = (before[2] - after_blanks) if after_blanks is not None else None
    status = "success" if after_blanks == 0 else "failed"
    return {
        **base, "status": status, "command_executed": audit_cmd,
        "message": (f"Converted {cleared} blank value(s) to NULL." if status == "success"
                    else "Update ran but blanks still remain — verify manually."),
        "before_metrics": {"total_rows": before[0], "blank_or_spaces": before[2]},
        "after_metrics":  {"total_rows": after[0] if after else None, "blank_or_spaces": after_blanks},
    }


def execute(
    conn: pyodbc.Connection,
    target_schema: str | None = None,
    target_table:  str | None = None,
    target_column: str | None = None,
) -> dict:
    """
    Convert blank/whitespace-only values to NULL in nullable text columns.
    Specific target → that column only; otherwise every eligible (nullable,
    contaminated) column from analyze() is processed.
    """
    targets = []
    if target_schema and target_table and target_column:
        targets.append((target_schema, target_table, target_column))
    else:
        analysis = analyze(conn)
        for f in analysis.get("affected_objects", []):
            if f.get("eligible"):
                targets.append((f["schema"], f["table"], f["column"]))
        if not targets:
            return {"status": "skipped",
                    "message": "No eligible (nullable, contaminated) columns to clean.",
                    "results": []}

    results, success, fail = [], 0, 0
    for sch, tbl, col in targets:
        res = _process_single(conn, sch, tbl, col)
        results.append(res)
        if res["status"] == "success":
            success += 1
        elif res["status"] == "failed":
            fail += 1

    status = "success"
    if fail:
        status = "partial" if success else "failed"
    msg = (results[0]["message"] if len(targets) == 1
           else f"Processed {len(targets)} column(s): {success} cleaned, {fail} failed.")
    return {"status": status, "message": msg, "results": results}
