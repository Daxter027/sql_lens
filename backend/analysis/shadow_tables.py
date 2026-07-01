"""
shadow_tables.py
----------------
Analysis module for Problem 20: Structural Twin Tables & Shadow Copies.

Finds obsolete backup/temp/legacy copy tables (by name pattern), reports their
size, age, row count, a heuristic active "counterpart", and a dependency count.

TWO HARD RULES (permanent, not "not implemented yet"):
  1. This tool NEVER executes DROP TABLE — under any circumstance, regardless of
     size, age, or how clean the dependency check looks. Removal is always a
     manual DBA decision made outside this tool. See execute() below.
  2. A clean dependency search does NOT prove a table is safe to remove. This
     check cannot see application code, scheduled jobs, external reporting/BI
     tools, linked-server queries, or infrequent processes. Larger size raises
     how IMPORTANT a candidate is to investigate — never how SAFE it is to drop.

The one executable action is a QUARANTINE RENAME (quarantine()): rename the
table with a dated suffix. It is fully reversible (rename back in seconds) and
is a more reliable real-world usage test than any static search — a real hidden
dependency fails loudly and immediately instead of silently as a DROP would.
"""

from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Any
import pyodbc
from config import (
    SHADOW_TABLE_NAME_PATTERNS,
    SHADOW_TABLE_SUFFIX_HINTS,
    SHADOW_QUARANTINE_SUFFIX,
    SHADOW_MAX_CANDIDATES,
)

logger = logging.getLogger(__name__)

ISSUE_ID   = "shadow_tables"
ISSUE_NAME = "Structural Twin Tables & Shadow Copies"


def _q(identifier: str) -> str:
    """Bracket-quote a SQL identifier, escaping any embedded close-bracket."""
    return "[" + identifier.replace("]", "]]") + "]"


def _discover(cursor) -> list[dict]:
    """Tables whose name matches a shadow/backup/temp pattern, with size & age."""
    like_clause = " OR ".join("t.name LIKE ?" for _ in SHADOW_TABLE_NAME_PATTERNS)
    params = [f"%{p}%" for p in SHADOW_TABLE_NAME_PATTERNS]
    cursor.execute(f"""
        SELECT s.name AS schema_name, t.name AS table_name, t.object_id,
               t.create_date, t.modify_date,
               ISNULL(ps.reserved_mb, 0)  AS reserved_mb,
               ISNULL(ps.row_count, 0)    AS row_count
        FROM sys.tables t
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        OUTER APPLY (
            SELECT CAST(SUM(p.reserved_page_count) * 8.0 / 1024 AS DECIMAL(18,2)) AS reserved_mb,
                   SUM(CASE WHEN p.index_id IN (0,1) THEN p.row_count ELSE 0 END) AS row_count
            FROM sys.dm_db_partition_stats p
            WHERE p.object_id = t.object_id
        ) ps
        WHERE t.is_ms_shipped = 0 AND ({like_clause})
        ORDER BY ISNULL(ps.reserved_mb, 0) DESC, t.name
    """, *params)
    out = []
    for r in cursor.fetchall():
        out.append({
            "schema": r[0], "table": r[1], "object_id": r[2],
            "create_date": str(r[3]) if r[3] else None,
            "modify_date": str(r[4]) if r[4] else None,
            "size_mb": float(r[5]) if r[5] is not None else 0.0,
            "row_count": int(r[6]) if r[6] is not None else 0,
        })
    return out


def _all_table_names(cursor) -> set:
    """All user table names as lowercase 'schema.table' — checked in-memory so
    counterpart lookups don't cost one query per candidate."""
    cursor.execute("""
        SELECT s.name, t.name FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id
        WHERE t.is_ms_shipped = 0
    """)
    return {f"{r[0].lower()}.{r[1].lower()}" for r in cursor.fetchall()}


def _guess_counterpart(schema: str, table: str, name_set: set) -> dict:
    """Heuristically guess an active counterpart by stripping a known suffix."""
    low = table.lower()
    base = None
    for suf in SHADOW_TABLE_SUFFIX_HINTS:
        if low.endswith(suf):
            base = table[: len(table) - len(suf)]
            break
    if base is None:
        return {"counterpart_guess": None, "counterpart_exists": False}
    exists = f"{schema.lower()}.{base.lower()}" in name_set
    return {"counterpart_guess": base, "counterpart_exists": bool(exists)}


def _dependency_counts(cursor, candidates: list[dict]) -> dict:
    """
    Dependency counts for ALL candidates in O(1) queries via catalog views
    (sys.sql_expression_dependencies for module refs + sys.foreign_keys), rather
    than a per-candidate sys.sql_modules text scan (which is very slow at scale).
    Returns {object_id: count}.
    NOTE: still NOT exhaustive — cannot see app code, jobs, BI tools, linked
    servers, or dynamic SQL. Zero here does NOT mean safe to remove.
    """
    counts = {c["object_id"]: 0 for c in candidates}
    if not candidates:
        return counts
    names = [c["table"] for c in candidates]
    obj_by_name = {c["table"].lower(): c["object_id"] for c in candidates}
    obj_ids = [c["object_id"] for c in candidates]

    # Module references (views/procs/functions/triggers) — catalog, no text scan.
    try:
        ph = ",".join("?" for _ in names)
        cursor.execute(f"""
            SELECT referenced_entity_name, COUNT(DISTINCT referencing_id)
            FROM sys.sql_expression_dependencies
            WHERE referenced_entity_name IN ({ph})
            GROUP BY referenced_entity_name
        """, *names)
        for r in cursor.fetchall():
            oid = obj_by_name.get((r[0] or "").lower())
            if oid is not None:
                counts[oid] += int(r[1])
    except pyodbc.Error:
        pass

    # Foreign-key references in either direction.
    try:
        ph = ",".join("?" for _ in obj_ids)
        cursor.execute(f"""
            SELECT oid, COUNT(*) FROM (
                SELECT referenced_object_id AS oid FROM sys.foreign_keys WHERE referenced_object_id IN ({ph})
                UNION ALL
                SELECT parent_object_id     AS oid FROM sys.foreign_keys WHERE parent_object_id     IN ({ph})
            ) x GROUP BY oid
        """, *obj_ids, *obj_ids)
        for r in cursor.fetchall():
            if int(r[0]) in counts:
                counts[int(r[0])] += int(r[1])
    except pyodbc.Error:
        pass

    return counts


def analyze(conn: pyodbc.Connection) -> dict[str, Any]:
    cursor = conn.cursor()
    candidates = _discover(cursor)          # already sorted by size DESC
    truncated = len(candidates) > SHADOW_MAX_CANDIDATES
    candidates = candidates[:SHADOW_MAX_CANDIDATES]
    name_set = _all_table_names(cursor)     # one query; counterpart checked in-memory
    dep_counts = _dependency_counts(cursor, candidates)  # O(1) queries, no text scan

    findings = []
    for c in candidates:
        cp = _guess_counterpart(c["schema"], c["table"], name_set)
        dep = dep_counts.get(c["object_id"], 0)
        findings.append({
            "schema":             c["schema"],
            "table":              c["table"],
            "size_mb":            c["size_mb"],
            "row_count":          c["row_count"],
            "create_date":        c["create_date"],
            "modify_date":        c["modify_date"],
            "dependency_count":   dep,
            "counterpart_guess":  cp["counterpart_guess"],
            "counterpart_exists": cp["counterpart_exists"],
        })

    total_size = round(sum(f["size_mb"] for f in findings), 2)
    note = (
        "Size sorts candidates by how important they are to INVESTIGATE — not how "
        "safe they are to remove. The dependency count is a non-exhaustive text "
        "search: it cannot see application code, jobs, BI/reporting tools, "
        "linked-server queries, or dynamic SQL, so zero dependencies does NOT mean "
        "safe to remove. This tool never drops tables; the only action offered is a "
        "reversible quarantine rename."
    )
    if truncated:
        note += (f" Showing the {SHADOW_MAX_CANDIDATES} largest candidates; "
                 "smaller ones were omitted this run.")

    if not findings:
        return {
            "issue_id":   ISSUE_ID,
            "issue_name": ISSUE_NAME,
            "severity":   "Low",
            "affected_objects": [],
            "current_metrics": {"candidate_count": 0, "total_size_mb": 0},
            "recommended_action": "No backup/temp/legacy-named tables found in the schema.",
            "estimated_impact": "N/A",
            "executable":       False,
            "eligible_for_fix": False,
            "blocking_reason":  "No shadow-table candidates found.",
            "analysis_note":    note,
        }

    severity = "High" if total_size > 1_000 else "Medium" if total_size > 50 else "Low"

    return {
        "issue_id":   ISSUE_ID,
        "issue_name": ISSUE_NAME,
        "severity":   severity,
        "affected_objects": findings,
        "current_metrics": {
            "candidate_count": len(findings),
            "total_size_mb":   total_size,
        },
        "recommended_action": (
            f"Found {len(findings)} possible shadow/legacy table(s) totalling ~{total_size:.0f} MB. "
            "Review the size, age, row count, counterpart match, and dependency count as INPUTS to "
            "a human decision. The only action this tool offers is a reversible QUARANTINE RENAME, "
            "which surfaces any hidden dependency loudly before a DBA ever considers removal. "
            "Actual removal is always a separate manual decision made outside this tool."
        ),
        "estimated_impact": "Reduced schema clutter and clearer data lineage once obsolete copies are quarantined/removed by a DBA.",
        # The actionable, reversible step is per-table quarantine (handled in the UI),
        # NOT a batch issue-level fix — so eligible_for_fix stays false here.
        "executable":       True,
        "eligible_for_fix": False,
        "blocking_reason":  None,
        "analysis_note":    note,
    }


def quarantine(
    conn: pyodbc.Connection,
    target_schema: str | None = None,
    target_table:  str | None = None,
    stamp: str | None = None,
) -> dict:
    """
    Rename a candidate table to a dated quarantine name. Fully reversible.
    This is NOT deletion — data and structure remain intact under the new name.
    """
    base = {"schema": target_schema, "table": target_table,
            "command_executed": None, "before_metrics": None, "after_metrics": None}
    if not (target_schema and target_table):
        return {**base, "status": "failed",
                "message": "A specific schema and table are required to quarantine."}

    cursor = conn.cursor()

    # ── Pre-check: table exists ───────────────────────────────────────────────
    cursor.execute("""
        SELECT COUNT(*) FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id
        WHERE s.name = ? AND t.name = ?
    """, target_schema, target_table)
    if cursor.fetchone()[0] == 0:
        return {**base, "status": "skipped",
                "message": f"[{target_schema}].[{target_table}] no longer exists."}

    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%d")
    new_name = f"{target_table}{SHADOW_QUARANTINE_SUFFIX}{stamp}"

    # ── Pre-check: target name free ──────────────────────────────────────────
    cursor.execute("""
        SELECT COUNT(*) FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id
        WHERE s.name = ? AND t.name = ?
    """, target_schema, new_name)
    if cursor.fetchone()[0] > 0:
        return {**base, "status": "skipped",
                "message": f"Quarantine name [{new_name}] already exists — skipped."}

    # ── Permission check ─────────────────────────────────────────────────────
    try:
        cursor.execute("SELECT HAS_PERMS_BY_NAME(?, 'OBJECT', 'ALTER'), IS_SRVROLEMEMBER('sysadmin')",
                       f"{target_schema}.{target_table}")
        has_alter, is_sa = cursor.fetchone()
        if not has_alter and not is_sa:
            return {**base, "status": "skipped", "message": "Current login lacks ALTER permission on the table."}
    except pyodbc.Error:
        pass

    audit_cmd = f"EXEC sp_rename '[{target_schema}].[{target_table}]', '{new_name}'"
    try:
        conn.autocommit = True
        # sp_rename: old name is schema-qualified; new name is the bare object name.
        cursor.execute("EXEC sp_rename ?, ?, 'OBJECT'",
                       f"{_q(target_schema)}.{_q(target_table)}", new_name)
        conn.autocommit = False
    except pyodbc.Error:
        conn.autocommit = False
        logger.error("sp_rename quarantine failed (details not forwarded to client)")
        return {**base, "status": "failed", "command_executed": audit_cmd,
                "message": "Quarantine rename failed.",
                "before_metrics": {"name": target_table}}

    # ── Post-verify ──────────────────────────────────────────────────────────
    cursor.execute("""
        SELECT COUNT(*) FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id
        WHERE s.name = ? AND t.name = ?
    """, target_schema, new_name)
    renamed = cursor.fetchone()[0] > 0
    if not renamed:
        return {**base, "status": "failed", "command_executed": audit_cmd,
                "message": "Rename ran but the new name was not found on verification.",
                "before_metrics": {"name": target_table}}

    logger.info("Quarantined [%s].[%s] -> [%s].", target_schema, target_table, new_name)
    return {
        **base, "status": "success", "command_executed": audit_cmd,
        "message": (f"Quarantined: renamed to [{new_name}]. This is NOT deletion — data and "
                    "structure are fully intact under the new name. Rename back to restore. "
                    "Any actual removal is a separate manual DBA decision outside this tool."),
        "before_metrics": {"name": target_table},
        "after_metrics":  {"name": new_name, "quarantined_at": stamp},
    }


def execute(*args, **kwargs):
    """Actual table removal is permanently out of scope — never wired."""
    raise NotImplementedError(
        "Table removal is permanently out of scope for automated execution in this "
        "tool — DROP TABLE is never performed automatically under any circumstance."
    )
