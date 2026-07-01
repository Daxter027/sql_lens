"""
archival_candidates.py
----------------------
Analysis module: AI-Powered Legacy Table Archival Candidate Detection.

Consumes the two result sets produced by archival_candidate_analysis.sql and
classifies each structurally-isolated table into a confidence bucket using a
DETERMINISTIC, rule-based additive score — no LLM, no external calls, no
randomness. Given the same input row (and `now`), classify_table() always
returns the same output.

SAFETY / SCOPE:
  * READ-ONLY. This module never executes, suggests executing, or wires up any
    DROP/ARCHIVE/DELETE. "Archive Candidate" is a *label in the report*, not an
    action. execute() is intentionally absent.
  * It never claims a table is "unused" — only a confidence classification with
    a transparent, reproducible reason. Final archival decisions always require
    business approval (stated in the output via DISCLAIMER).
  * Structural isolation (no FK / SP / function / view / trigger references) is a
    PRECONDITION enforced upstream by the SQL script — it is NOT a scoring input.

Scoring ceiling is 70 (not 100): 25 (>10yr) + 15 (>15yr) + 15 (naming) +
10 (reserved>100MB) + 5 (rows>1000). See classify_table / SPEC §3.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import pyodbc

logger = logging.getLogger(__name__)

ISSUE_ID   = "archival_candidates"
ISSUE_NAME = "Legacy Table Archival Candidate Detection"

_SQL_PATH = Path(__file__).parent / "sql" / "archival_candidate_analysis.sql"

# Verbatim, unmodified — must appear in every response payload (SPEC §4).
DISCLAIMER = (
    "This module generates archival recommendations based on automated metadata "
    "analysis only. It does not and cannot prove a table is unused. Final archival "
    "decisions always require business approval."
)

# Known placeholder / sentinel date *components* (SPEC §3.3).
PLACEHOLDER_DATES = {date(1900, 1, 1), date(1940, 1, 1), date(1956, 1, 1), date(1980, 1, 1)}

# Naming tokens (case-insensitive). Substring tokens match anywhere; boundary
# tokens require a delimiter/word boundary to avoid false positives such as
# "LogisticsOrders" or "BKashTransactions" (SPEC §3.3).
NAMING_SUBSTRING_TOKENS = ["OLD", "Backup", "Archive", "Temp", "History"]
NAMING_BOUNDARY_TOKENS  = ["BK", "Log"]

# Bucket labels.
BUCKET_VERY_HIGH = "Very High Confidence Archive Candidate"
BUCKET_HIGH      = "High Confidence Archive Candidate"
BUCKET_VALIDATE  = "Requires Business Validation"
BUCKET_ACTIVE    = "Probably Active"
BUCKET_IGNORE    = "Ignore"
BUCKET_NO_ANALYZE = "Could Not Analyze"

SUGGESTED_ACTION = {
    BUCKET_VERY_HIGH:  "Archive Candidate",
    BUCKET_HIGH:       "Archive Candidate",
    BUCKET_VALIDATE:   "Investigate with Business Team",
    BUCKET_ACTIVE:     "Keep Active",
    BUCKET_IGNORE:     "Ignore",
    BUCKET_NO_ANALYZE: "Investigate with Business Team",
}

STRUCTURAL_STATUS = (
    "No FK, stored procedure, function, view, or trigger references detected "
    "(verified upstream by analysis query)"
)


@dataclass
class TableMetadataRow:
    """One row of Result Set 1, plus failure status derived from Result Set 2."""
    schema_name: str
    table_name: str
    overall_earliest_date: Optional[datetime]
    overall_latest_date: Optional[datetime]
    years_since_latest_date: Optional[float]   # consumed as-is; never recomputed
    total_rows: int
    reserved_mb: float
    data_mb: float
    index_mb: float
    unused_mb: float
    failed: bool = False                        # appeared in Result Set 2
    error_message: Optional[str] = None         # verbatim from Result Set 2


# ─────────────────────────────────────────────────────────────────────────────
# Naming / date helpers (pure)
# ─────────────────────────────────────────────────────────────────────────────

def _boundary_match(name: str, token: str) -> bool:
    """
    True if `token` occurs in `name` (case-insensitive) bounded by a delimiter
    or word boundary: the char before must not be a letter, and the char after
    must not be a lowercase letter (so camelCase like 'LogTable' matches but
    'LogisticsOrders' does not; '_BK'/'Orders_BK' match but 'BKashTransactions'
    does not).
    """
    lname, ltok = name.lower(), token.lower()
    start = 0
    while True:
        idx = lname.find(ltok, start)
        if idx == -1:
            return False
        left_ok = idx == 0 or not name[idx - 1].isalpha()
        end = idx + len(ltok)
        right_ok = end == len(name) or not name[end].islower()
        if left_ok and right_ok:
            return True
        start = idx + 1


def matched_naming_tokens(table_name: str) -> list[str]:
    """Return every naming token that fires for this table name (for transparency)."""
    flags: list[str] = []
    low = table_name.lower()
    for tok in NAMING_SUBSTRING_TOKENS:
        if tok.lower() in low:
            flags.append(tok)
    for tok in NAMING_BOUNDARY_TOKENS:
        if _boundary_match(table_name, tok):
            flags.append(tok)
    return flags


def _as_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _is_placeholder(earliest: Optional[date], latest: Optional[date]) -> bool:
    return (earliest in PLACEHOLDER_DATES) or (latest in PLACEHOLDER_DATES)


def _is_invalid(earliest: Optional[date], latest: Optional[date], now: datetime) -> bool:
    """Impossible-future latest date (>1yr ahead) or earliest > latest inversion."""
    now_date = now.date() if isinstance(now, datetime) else now
    if latest is not None and (latest - now_date).days > 365:
        return True
    if earliest is not None and latest is not None and earliest > latest:
        return True
    return False


def bucket_for_score(raw: int) -> str:
    """Bucket thresholds rescaled to the real 0–70 ceiling (SPEC §3.4)."""
    if raw >= 60:
        return BUCKET_VERY_HIGH
    if raw >= 45:
        return BUCKET_HIGH
    if raw >= 28:
        return BUCKET_VALIDATE
    if raw >= 13:
        return BUCKET_ACTIVE
    return BUCKET_IGNORE


def risk_level(bucket: str, total_rows: int, reserved_mb: float, data_quality_flag: bool) -> str:
    """
    Blast radius if this table were wrongly archived (SPEC §3.6). Ordered,
    first-match-wins. NOTE: the High rule is evaluated before the Medium rule so
    it can actually fire — under the literal table order the Medium rule
    (ReservedMB >= 100) would always pre-empt the High rule (top bucket AND
    ReservedMB >= 1000). Flagged as a spec-ordering correction.
    """
    top_two = bucket in (BUCKET_VERY_HIGH, BUCKET_HIGH)
    if total_rows == 0:
        base = "Low"
    elif bucket in (BUCKET_IGNORE, BUCKET_ACTIVE):
        base = "Low"
    elif top_two and reserved_mb >= 1000:
        base = "High"
    elif reserved_mb >= 100 or total_rows > 1_000_000:
        base = "Medium"
    elif reserved_mb < 100 and not top_two:
        base = "Low"
    else:
        base = "Low"   # small top-bucket table → low blast radius
    if data_quality_flag:
        base = {"Low": "Medium", "Medium": "High", "High": "High"}[base]
    return base


# ─────────────────────────────────────────────────────────────────────────────
# Core deterministic classifier (pure — no I/O, no datetime.now())
# ─────────────────────────────────────────────────────────────────────────────

def classify_table(row: TableMetadataRow, now: datetime) -> dict[str, Any]:
    """
    Classify a single table into a confidence bucket. Pure: identical (row, now)
    always yields identical output. `now` is injected (never read internally) so
    the future-date sanity check stays reproducible in tests.
    """
    naming_flags = matched_naming_tokens(row.table_name)
    storage = {
        "total_rows":  int(row.total_rows or 0),
        "reserved_mb": float(row.reserved_mb or 0),
        "data_mb":     float(row.data_mb or 0),
        "index_mb":    float(row.index_mb or 0),
        "unused_mb":   float(row.unused_mb or 0),
    }
    empty_points = {
        "age_over_10_years": 0, "age_over_15_years": 0, "naming_convention": 0,
        "reserved_space_over_100mb": 0, "row_count_over_1000": 0,
        "placeholder_date_penalty": 0, "invalid_or_impossible_date_penalty": 0,
    }

    def build(*, score, bucket, points, dq_flag, dq_notes, reason):
        return {
            "schema_name": row.schema_name,
            "table_name":  row.table_name,
            "confidence_score": score,
            "confidence_bucket": bucket,
            "points_breakdown": points,
            "years_since_latest_activity":
                float(row.years_since_latest_date) if row.years_since_latest_date is not None else None,
            "earliest_business_date": _as_date(row.overall_earliest_date).isoformat()
                if _as_date(row.overall_earliest_date) else None,
            "latest_business_date": _as_date(row.overall_latest_date).isoformat()
                if _as_date(row.overall_latest_date) else None,
            "storage": storage,
            "structural_dependency_status": STRUCTURAL_STATUS,
            "naming_convention_flags": naming_flags,
            "data_quality_flag": dq_flag,
            "data_quality_notes": dq_notes,
            "reason": reason,
            "risk_level": risk_level(bucket, storage["total_rows"], storage["reserved_mb"], dq_flag),
            "suggested_action": SUGGESTED_ACTION[bucket],
        }

    # ── Case B — appeared in the failed-tables result set. No points. ─────────
    if row.failed:
        msg = row.error_message or "unknown error"
        return build(
            score=None, bucket=BUCKET_NO_ANALYZE, points=dict(empty_points),
            dq_flag=False, dq_notes=None,
            reason=(f"Date analysis failed during metadata collection: {msg}. "
                    "Recommend manual review. No confidence score computed."),
        )

    earliest = _as_date(row.overall_earliest_date)
    latest   = _as_date(row.overall_latest_date)

    # ── Case A — no datetime columns at all (and not failed). Score null. ─────
    if row.overall_earliest_date is None and row.overall_latest_date is None:
        points = dict(empty_points)
        if naming_flags:
            points["naming_convention"] = 15
        if storage["reserved_mb"] > 100:
            points["reserved_space_over_100mb"] = 10
        if storage["total_rows"] > 1000:
            points["row_count_over_1000"] = 5
        return build(
            score=None, bucket=BUCKET_VALIDATE, points=points,
            dq_flag=False, dq_notes=None,
            reason=("Table has no date/datetime columns to evaluate; freshness "
                    "cannot be determined from data alone. No confidence score computed."),
        )

    # ── Cases C / D — usable date(s); run additive point scoring (SPEC §3.3) ──
    years = row.years_since_latest_date
    placeholder = _is_placeholder(earliest, latest)
    invalid     = _is_invalid(earliest, latest, now)

    points = dict(empty_points)
    if years is not None and years > 10:
        points["age_over_10_years"] = 25
    if years is not None and years > 15:
        points["age_over_15_years"] = 15
    if naming_flags:
        points["naming_convention"] = 15
    if storage["reserved_mb"] > 100:
        points["reserved_space_over_100mb"] = 10
    if storage["total_rows"] > 1000:
        points["row_count_over_1000"] = 5
    if placeholder:
        points["placeholder_date_penalty"] = -20
    if invalid:
        points["invalid_or_impossible_date_penalty"] = -15

    raw = sum(points.values())
    raw = max(0, min(70, raw))           # clamp [0, 70]
    bucket = bucket_for_score(raw)
    dq_flag = placeholder or invalid

    dq_notes = None
    if dq_flag:
        notes = []
        if placeholder:
            ph = latest if latest in PLACEHOLDER_DATES else earliest
            notes.append(f"date {ph.isoformat()} matches a known placeholder pattern")
        if invalid:
            if latest is not None and (latest - (now.date() if isinstance(now, datetime) else now)).days > 365:
                notes.append(f"OverallLatestDate {latest.isoformat()} is more than a year in the future")
            if earliest is not None and latest is not None and earliest > latest:
                notes.append("OverallEarliestDate is later than OverallLatestDate (inversion)")
        dq_notes = "; ".join(notes)

    reason = _build_reason(row, points, raw, naming_flags, latest, placeholder, invalid)
    return build(score=raw, bucket=bucket, points=points,
                 dq_flag=dq_flag, dq_notes=dq_notes, reason=reason)


def _build_reason(row, points, raw, naming_flags, latest, placeholder, invalid) -> str:
    """Compose the human-readable reason from the signals that actually fired."""
    parts: list[str] = []
    years = row.years_since_latest_date
    latest_str = latest.isoformat() if latest else "unknown date"

    if points["age_over_10_years"]:
        seg = (f"No business activity detected since {latest_str} "
               f"(~{years} years ago), scoring +25 (>10 years)")
        if points["age_over_15_years"]:
            seg += " and +15 (>15 years)"
        parts.append(seg + ".")
    elif years is not None:
        parts.append(f"Most recent date signal is {latest_str} (~{years} years ago); "
                     "not old enough for age-based points.")

    if naming_flags:
        toks = ", ".join(f"'{t}'" for t in naming_flags)
        parts.append(f"Table name matches archival naming pattern(s) {toks} (+15).")

    if points["reserved_space_over_100mb"]:
        parts.append(f"Reserved space is {row.reserved_mb:.0f} MB (+10).")
    if points["row_count_over_1000"]:
        parts.append(f"Row count is {int(row.total_rows):,} (+5).")

    if placeholder:
        parts.append(
            f"Latest/earliest recorded date matches a known placeholder pattern "
            f"(-20 applied). Age-based scoring is unreliable as a result — flagged "
            f"for business validation rather than treated as confirmed-old.")
    if invalid:
        parts.append(
            "Dates appear invalid or impossible (future-dated or earliest>latest) "
            "(-15 applied); treat the age signal with caution.")

    parts.append(f"Total: {raw}/70.")
    parts.append("No structural dependencies found by analysis query.")
    return " ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Ingestion layer — run the SQL script, parse both result sets, assemble output
# ─────────────────────────────────────────────────────────────────────────────

def _load_sql() -> str:
    return _SQL_PATH.read_text(encoding="utf-8")


def _collect_result_sets(cursor) -> list[tuple[list[str], list]]:
    """Gather every result set (columns, rows) the batch returned."""
    sets: list[tuple[list[str], list]] = []
    while True:
        if cursor.description is not None:
            cols = [d[0] for d in cursor.description]
            try:
                rows = cursor.fetchall()
            except pyodbc.Error:
                rows = []
            sets.append((cols, rows))
        if not cursor.nextset():
            break
    return sets


def _parse_result_sets(sets) -> tuple[list[dict], dict[tuple[str, str], str]]:
    """
    Split collected result sets into the per-table report (Result Set 1) and a
    {(schema, table): error_message} map (Result Set 2). A lone FailedTablesStatus
    row means "no failures".
    """
    report: list[dict] = []
    failed: dict[tuple[str, str], str] = {}
    for cols, rows in sets:
        cset = set(cols)
        if {"OverallLatestDate", "YearsSinceLatestDate"} & cset:
            for r in rows:
                d = dict(zip(cols, r))
                report.append(d)
        elif {"SchemaName", "TableName", "ErrorMessage"} <= cset:
            for r in rows:
                d = dict(zip(cols, r))
                failed[(d["SchemaName"], d["TableName"])] = d["ErrorMessage"]
        # FailedTablesStatus (no-failure marker) and any stray sets are ignored.
    return report, failed


def analyze(conn: pyodbc.Connection) -> dict[str, Any]:
    cursor = conn.cursor()
    cursor.execute(_load_sql())
    report_rows, failed_map = _parse_result_sets(_collect_result_sets(cursor))

    now = datetime.now()   # ingestion-layer clock; classify_table receives it explicitly
    classifications: list[dict] = []
    for d in report_rows:
        key = (d.get("SchemaName"), d.get("TableName"))
        row = TableMetadataRow(
            schema_name=d.get("SchemaName"),
            table_name=d.get("TableName"),
            overall_earliest_date=d.get("OverallEarliestDate"),
            overall_latest_date=d.get("OverallLatestDate"),
            years_since_latest_date=(float(d["YearsSinceLatestDate"])
                                     if d.get("YearsSinceLatestDate") is not None else None),
            total_rows=int(d.get("TotalRows") or 0),
            reserved_mb=float(d.get("ReservedMB") or 0),
            data_mb=float(d.get("DataMB") or 0),
            index_mb=float(d.get("IndexMB") or 0),
            unused_mb=float(d.get("UnusedMB") or 0),
            failed=key in failed_map,
            error_message=failed_map.get(key),
        )
        classifications.append(classify_table(row, now))

    # Sort: highest numeric score first, null scores (A/B) after.
    classifications.sort(key=lambda c: (c["confidence_score"] is None,
                                        -(c["confidence_score"] or 0),
                                        c["schema_name"], c["table_name"]))

    counts = {b: 0 for b in (BUCKET_VERY_HIGH, BUCKET_HIGH, BUCKET_VALIDATE,
                             BUCKET_ACTIVE, BUCKET_IGNORE, BUCKET_NO_ANALYZE)}
    for c in classifications:
        counts[c["confidence_bucket"]] = counts.get(c["confidence_bucket"], 0) + 1

    failed_tables = [{"schema_name": s, "table_name": t, "error_message": e}
                     for (s, t), e in sorted(failed_map.items())]

    top_two = [c for c in classifications
               if c["confidence_bucket"] in (BUCKET_VERY_HIGH, BUCKET_HIGH)]
    top_reserved_mb = round(sum(c["storage"]["reserved_mb"] for c in top_two), 2)

    metrics = {
        "total_candidates":   len(classifications),
        "very_high":          counts[BUCKET_VERY_HIGH],
        "high":               counts[BUCKET_HIGH],
        "requires_validation": counts[BUCKET_VALIDATE],
        "probably_active":    counts[BUCKET_ACTIVE],
        "ignore":             counts[BUCKET_IGNORE],
        "could_not_analyze":  counts[BUCKET_NO_ANALYZE],
        "failed_count":       len(failed_tables),
        "failed_tables":      failed_tables,
        "score_scale":        "0-70",
        "disclaimer":         DISCLAIMER,
    }

    if not classifications:
        return {
            "issue_id": ISSUE_ID, "issue_name": ISSUE_NAME, "severity": "Low",
            "affected_objects": [], "current_metrics": metrics,
            "recommended_action": ("No structurally-isolated tables matched the "
                                   "archival-candidate analysis. " + DISCLAIMER),
            "estimated_impact": "N/A",
            "executable": False, "eligible_for_fix": False, "blocking_reason": None,
            "analysis_note": ("Deterministic rule-based scoring (0–70 scale). Structural "
                              "isolation pre-filtered by the analysis query. Read-only."),
        }

    if counts[BUCKET_VERY_HIGH]:
        severity = "High"
    elif counts[BUCKET_HIGH] or counts[BUCKET_VALIDATE]:
        severity = "Medium"
    else:
        severity = "Low"

    recommended = (
        f"Identified {len(classifications)} structurally-isolated table(s): "
        f"{counts[BUCKET_VERY_HIGH]} very-high / {counts[BUCKET_HIGH]} high confidence "
        f"archive candidate(s), {counts[BUCKET_VALIDATE]} requiring business validation. "
        "Scores are 0–70 (see methodology); 'Archive Candidate' is a label, not an action. "
        + DISCLAIMER
    )

    return {
        "issue_id": ISSUE_ID, "issue_name": ISSUE_NAME, "severity": severity,
        "affected_objects": classifications,
        "current_metrics": metrics,
        "recommended_action": recommended,
        "estimated_impact": (
            f"~{top_reserved_mb:,.0f} MB across {len(top_two)} high-confidence "
            "candidate(s), reclaimable only after business-approved archival."
            if top_two else "N/A"),
        "executable": False, "eligible_for_fix": False, "blocking_reason": None,
        "analysis_note": ("Deterministic rule-based scoring (0–70 scale — see methodology). "
                          "Structural isolation pre-filtered by the analysis query. "
                          "Read-only — no archival action is performed."),
    }


# This module is intentionally analysis-only. There is deliberately no execute().
