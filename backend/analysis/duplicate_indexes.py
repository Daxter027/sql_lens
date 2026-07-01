"""
duplicate_indexes.py
--------------------
Finds redundant indexes that waste space and slow down writes:
  • EXACT duplicates  — two indexes with the identical ordered key columns.
  • PREFIX overlaps   — a non-unique index whose key columns are a strict prefix
                        of another index (the longer one already serves prefix seeks).

READ-ONLY / ANALYSIS-ONLY. It reports the redundant index + a DROP script, but
never drops anything (and never flags a PK/unique index as the droppable one —
those enforce constraints). Dropping is a deliberate DBA decision.
"""

from __future__ import annotations
import logging
from typing import Any
import pyodbc

logger = logging.getLogger(__name__)

ISSUE_ID   = "duplicate_indexes"
ISSUE_NAME = "Duplicate & Overlapping Indexes"

_COLS_SQL = """
SELECT
    i.object_id, i.index_id, s.name AS schema_name, t.name AS table_name,
    i.name AS index_name, i.type_desc, i.is_primary_key, i.is_unique_constraint, i.is_unique,
    ic.key_ordinal, ic.is_included_column, c.name AS col_name
FROM sys.indexes i
JOIN sys.tables  t ON t.object_id = i.object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
WHERE t.is_ms_shipped = 0
  AND i.index_id > 0
  AND i.type_desc IN ('CLUSTERED', 'NONCLUSTERED')
ORDER BY i.object_id, i.index_id, ic.is_included_column, ic.key_ordinal
"""

_SIZE_SQL = """
SELECT object_id, index_id, SUM(reserved_page_count) * 8 / 1024.0 AS mb
FROM sys.dm_db_partition_stats
GROUP BY object_id, index_id
"""


def _load_indexes(cursor) -> dict:
    """Assemble one record per index with ordered key columns + include set."""
    cursor.execute(_COLS_SQL)
    idx: dict[tuple, dict] = {}
    for (object_id, index_id, schema, table, name, type_desc,
         is_pk, is_uc, is_unique, key_ordinal, is_included, col) in cursor.fetchall():
        key = (object_id, index_id)
        rec = idx.get(key)
        if rec is None:
            rec = idx[key] = {
                "object_id": object_id, "index_id": index_id,
                "schema": schema, "table": table, "name": name, "type_desc": type_desc,
                "is_pk": bool(is_pk), "is_uc": bool(is_uc), "is_unique": bool(is_unique),
                "keys": [], "includes": set(),
            }
        if is_included:
            rec["includes"].add(col)
        else:
            rec["keys"].append(col)
    return idx


def _keeper(a: dict, b: dict) -> dict:
    """Of two identical indexes, which one to KEEP (never drop a constraint index)."""
    for r in (a, b):
        if r["is_pk"] or r["is_uc"] or r["is_unique"] or r["type_desc"] == "CLUSTERED":
            return r
    return a   # neither special — keep the first arbitrarily


def analyze(conn: pyodbc.Connection) -> dict[str, Any]:
    cursor = conn.cursor()
    idx = _load_indexes(cursor)

    cursor.execute(_SIZE_SQL)
    sizes = {(oid, iid): float(mb or 0) for (oid, iid, mb) in cursor.fetchall()}

    # Group indexes per table.
    by_table: dict[tuple, list] = {}
    for rec in idx.values():
        by_table.setdefault((rec["schema"], rec["table"]), []).append(rec)

    findings = []
    for (schema, table), indexes in by_table.items():
        # ── exact duplicates: identical ordered key columns ──────────────────
        seen: dict[tuple, dict] = {}
        exact_flagged = set()
        for rec in indexes:
            sig = tuple(rec["keys"])
            if not sig:
                continue
            if sig in seen:
                keep = _keeper(seen[sig], rec)
                drop = rec if keep is seen[sig] else seen[sig]
                seen[sig] = keep
                if drop["is_pk"] or drop["is_uc"] or drop["is_unique"]:
                    continue  # never propose dropping a constraint index
                exact_flagged.add(drop["index_id"])
                findings.append(_finding(drop, keep, "Exact duplicate", sizes))
            else:
                seen[sig] = rec

        # ── prefix overlaps: a non-unique index is a strict prefix of another ─
        for a in indexes:
            if a["index_id"] in exact_flagged or a["is_pk"] or a["is_uc"] or a["is_unique"]:
                continue
            ak = a["keys"]
            for b in indexes:
                if a is b:
                    continue
                bk = b["keys"]
                if len(ak) < len(bk) and bk[:len(ak)] == ak:
                    findings.append(_finding(a, b, "Prefix overlap", sizes))
                    break

    total_mb = round(sum(f["size_mb"] for f in findings), 2)
    note = ("Exact duplicates are safe drop candidates. Prefix overlaps are lower "
            "confidence — the shorter index may still be preferred by the optimizer "
            "for some queries. Constraint (PK/unique) indexes are never flagged as droppable. "
            "Verify with the workload before dropping.")

    if not findings:
        return {
            "issue_id": ISSUE_ID, "issue_name": ISSUE_NAME, "severity": "Low",
            "affected_objects": [], "current_metrics": {"redundant_count": 0, "wasted_space_mb": 0},
            "recommended_action": "No duplicate or overlapping indexes found.",
            "estimated_impact": "N/A", "executable": False, "eligible_for_fix": False,
            "analysis_note": note,
        }

    severity = "Medium" if total_mb > 500 else "Low"
    return {
        "issue_id": ISSUE_ID, "issue_name": ISSUE_NAME, "severity": severity,
        "affected_objects": findings,
        "current_metrics": {
            "redundant_count": len(findings),
            "wasted_space_mb": total_mb,
            "exact_duplicates": sum(1 for f in findings if f["kind"] == "Exact duplicate"),
        },
        "recommended_action": (
            f"Found {len(findings)} redundant index(es) (~{total_mb:.0f} MB) that duplicate or "
            "overlap another index. Each row has a DROP script — start with exact duplicates, "
            "which are the safest to remove, and reclaim write throughput + space."
        ),
        "estimated_impact": f"~{total_mb:.0f} MB reclaimable + fewer index writes on affected tables.",
        "executable": False, "eligible_for_fix": False,
        "analysis_note": note,
    }


def _finding(drop: dict, keep: dict, kind: str, sizes: dict) -> dict:
    mb = sizes.get((drop["object_id"], drop["index_id"]), 0.0)
    return {
        "schema": drop["schema"], "table": drop["table"],
        "index": drop["name"], "kind": kind,
        "key_columns": ", ".join(drop["keys"]),
        "redundant_with": keep["name"],
        "size_mb": round(mb, 2),
        "drop_script": f"DROP INDEX [{drop['name']}] ON [{drop['schema']}].[{drop['table']}];",
    }
