"""
Unit tests for analysis/archival_candidates.py — the deterministic classifier.

No DB and no pytest required. Run directly:

    python backend/tests/test_archival_candidates.py

(pytest-compatible too: each test is a plain `test_*` function with asserts.)
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

# Allow `import analysis.archival_candidates` when run from anywhere.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from analysis.archival_candidates import (  # noqa: E402
    TableMetadataRow, classify_table, matched_naming_tokens, bucket_for_score,
    risk_level, BUCKET_VERY_HIGH, BUCKET_HIGH, BUCKET_VALIDATE, BUCKET_ACTIVE,
    BUCKET_IGNORE, BUCKET_NO_ANALYZE,
)

NOW = datetime(2026, 6, 26)   # fixed analysis clock for reproducibility


def _row(**kw):
    base = dict(
        schema_name="dbo", table_name="SomeTable",
        overall_earliest_date=None, overall_latest_date=None,
        years_since_latest_date=None,
        total_rows=0, reserved_mb=0.0, data_mb=0.0, index_mb=0.0, unused_mb=0.0,
        failed=False, error_message=None,
    )
    base.update(kw)
    return TableMetadataRow(**base)


def _years_ago(years: float) -> datetime:
    # A latest date roughly `years` before NOW (only used for date fields;
    # the score reads years_since_latest_date, passed explicitly).
    return datetime(int(2026 - years), 6, 26)


# ── Worked example 1 (SPEC §3.4): 16y, Orders_Old, 150MB, 50000 rows → 70 ────
def test_worked_example_very_high():
    r = _row(table_name="Orders_Old",
             overall_earliest_date=_years_ago(16), overall_latest_date=_years_ago(16),
             years_since_latest_date=16.0, total_rows=50000, reserved_mb=150.0)
    c = classify_table(r, NOW)
    assert c["confidence_score"] == 70, c["confidence_score"]
    assert c["confidence_bucket"] == BUCKET_VERY_HIGH
    pb = c["points_breakdown"]
    assert (pb["age_over_10_years"], pb["age_over_15_years"], pb["naming_convention"],
            pb["reserved_space_over_100mb"], pb["row_count_over_1000"]) == (25, 15, 15, 10, 5)


# ── Worked example 2 (SPEC §3.4): 11y, no naming, 50MB, 200 rows → 25 ────────
def test_worked_example_probably_active():
    r = _row(table_name="Customers",
             overall_earliest_date=_years_ago(11), overall_latest_date=_years_ago(11),
             years_since_latest_date=11.0, total_rows=200, reserved_mb=50.0)
    c = classify_table(r, NOW)
    assert c["confidence_score"] == 25
    assert c["confidence_bucket"] == BUCKET_ACTIVE


# ── Case A — no datetime columns → Requires Business Validation, score null ──
def test_case_a_no_dates():
    r = _row(table_name="LookupCodes", total_rows=5000, reserved_mb=200.0)
    c = classify_table(r, NOW)
    assert c["confidence_score"] is None
    assert c["confidence_bucket"] == BUCKET_VALIDATE
    # Storage/naming still reported for context.
    assert c["points_breakdown"]["reserved_space_over_100mb"] == 10
    assert c["points_breakdown"]["row_count_over_1000"] == 5
    assert "no date" in c["reason"].lower()


# ── Case B — failed table → Could Not Analyze, score null, no points ─────────
def test_case_b_failed():
    r = _row(table_name="Broken", failed=True,
             error_message="DateAnalysis: Conversion failed when converting datetime")
    c = classify_table(r, NOW)
    assert c["confidence_score"] is None
    assert c["confidence_bucket"] == BUCKET_NO_ANALYZE
    assert all(v == 0 for v in c["points_breakdown"].values())
    assert "Conversion failed" in c["reason"]


# ── Case C — placeholder date pulls score down; never reaches top bucket ─────
def test_case_c_placeholder_penalty():
    # 20y old, named, big, high rows, BUT latest date is 1900-01-01 placeholder.
    r = _row(table_name="Sales_Archive",
             overall_earliest_date=datetime(1900, 1, 1),
             overall_latest_date=datetime(1900, 1, 1),
             years_since_latest_date=20.0, total_rows=80000, reserved_mb=300.0)
    c = classify_table(r, NOW)
    # 25 + 15 + 15 + 10 + 5 = 70, minus 20 placeholder = 50.
    assert c["confidence_score"] == 50
    assert c["points_breakdown"]["placeholder_date_penalty"] == -20
    assert c["data_quality_flag"] is True
    # 50 lands in High (45-59), strictly below Very High (>=60) — penalty intent.
    assert c["confidence_bucket"] == BUCKET_HIGH
    assert c["confidence_bucket"] != BUCKET_VERY_HIGH


# ── Case D — clean usable signal, mid score ──────────────────────────────────
def test_case_d_usable():
    r = _row(table_name="Invoices",
             overall_earliest_date=_years_ago(12), overall_latest_date=_years_ago(12),
             years_since_latest_date=12.0, total_rows=2000, reserved_mb=120.0)
    c = classify_table(r, NOW)
    # 25 (>10) + 10 (reserved) + 5 (rows) = 40 → Requires Business Validation.
    assert c["confidence_score"] == 40
    assert c["confidence_bucket"] == BUCKET_VALIDATE
    assert c["data_quality_flag"] is False


# ── Invalid/impossible future date penalty ───────────────────────────────────
def test_invalid_future_date_penalty():
    r = _row(table_name="FutureData",
             overall_earliest_date=_years_ago(0), overall_latest_date=datetime(2030, 1, 1),
             years_since_latest_date=0.0, total_rows=500, reserved_mb=50.0)
    c = classify_table(r, NOW)
    assert c["points_breakdown"]["invalid_or_impossible_date_penalty"] == -15
    assert c["data_quality_flag"] is True


# ── Both penalties stack and the [0,70] clamp floors at 0 ────────────────────
def test_clamp_floor_zero():
    # earliest is placeholder (1900), latest is impossible future → both penalties,
    # no positive points (fresh-ish, small, unsuspicious name).
    r = _row(table_name="PlainTable",
             overall_earliest_date=datetime(1900, 1, 1),
             overall_latest_date=datetime(2031, 1, 1),
             years_since_latest_date=0.0, total_rows=10, reserved_mb=5.0)
    c = classify_table(r, NOW)
    assert c["points_breakdown"]["placeholder_date_penalty"] == -20
    assert c["points_breakdown"]["invalid_or_impossible_date_penalty"] == -15
    assert c["confidence_score"] == 0      # clamped, not negative
    assert c["confidence_bucket"] == BUCKET_IGNORE


# ── Placeholder + nominally ancient date never reaches top bucket (SPEC §6) ──
def test_placeholder_old_not_top_bucket():
    r = _row(table_name="Ledger_OLD",
             overall_earliest_date=datetime(1900, 1, 1),
             overall_latest_date=datetime(1900, 1, 1),
             years_since_latest_date=20.0, total_rows=2000, reserved_mb=150.0)
    c = classify_table(r, NOW)
    # 25+15+15+10+5 = 70, -20 = 50 → High, never Very High.
    assert c["confidence_bucket"] != BUCKET_VERY_HIGH


# ── Naming boundary semantics for BK / Log ───────────────────────────────────
def test_naming_boundary_matching():
    assert matched_naming_tokens("BKashTransactions") == []      # no false positive
    assert matched_naming_tokens("LogisticsOrders") == []        # no false positive
    assert "BK" in matched_naming_tokens("Orders_BK")
    assert "Log" in matched_naming_tokens("Sales_Log")
    assert "Log" in matched_naming_tokens("LogTable")            # camelCase boundary
    # Substring tokens still match anywhere.
    assert "Archive" in matched_naming_tokens("MyArchiveStuff")
    assert "History" in matched_naming_tokens("OrderHistory")


# ── Age stacking boundary semantics at exactly 10, 10.01, 15, 15.01 ──────────
def test_age_strict_greater_than_boundaries():
    def age_points(y):
        r = _row(table_name="T", overall_latest_date=_years_ago(int(y)),
                 overall_earliest_date=_years_ago(int(y)), years_since_latest_date=y,
                 total_rows=0, reserved_mb=0.0)
        pb = classify_table(r, NOW)["points_breakdown"]
        return pb["age_over_10_years"], pb["age_over_15_years"]

    assert age_points(10.0) == (0, 0)      # 10.0 does NOT trigger >10
    assert age_points(10.01) == (25, 0)    # just over 10
    assert age_points(15.0) == (25, 0)     # 15.0 triggers >10 but NOT >15
    assert age_points(15.01) == (25, 15)   # just over 15 → both stack = 40


# ── bucket_for_score boundaries ──────────────────────────────────────────────
def test_bucket_boundaries():
    assert bucket_for_score(70) == BUCKET_VERY_HIGH
    assert bucket_for_score(60) == BUCKET_VERY_HIGH
    assert bucket_for_score(59) == BUCKET_HIGH
    assert bucket_for_score(45) == BUCKET_HIGH
    assert bucket_for_score(44) == BUCKET_VALIDATE
    assert bucket_for_score(28) == BUCKET_VALIDATE
    assert bucket_for_score(27) == BUCKET_ACTIVE
    assert bucket_for_score(13) == BUCKET_ACTIVE
    assert bucket_for_score(12) == BUCKET_IGNORE
    assert bucket_for_score(0) == BUCKET_IGNORE


# ── Risk level: data_quality_flag bumps one level; top-bucket+big → High ─────
def test_risk_levels():
    assert risk_level(BUCKET_IGNORE, 5000, 50.0, False) == "Low"
    assert risk_level(BUCKET_VALIDATE, 0, 9999.0, False) == "Low"            # 0 rows
    assert risk_level(BUCKET_VALIDATE, 5000, 200.0, False) == "Medium"
    assert risk_level(BUCKET_VERY_HIGH, 5000, 1500.0, False) == "High"
    assert risk_level(BUCKET_VALIDATE, 5000, 200.0, True) == "High"          # Medium→High bump
    assert risk_level(BUCKET_ACTIVE, 5000, 50.0, True) == "Medium"          # Low→Medium bump


# ── Determinism: same input twice → identical output ─────────────────────────
def test_determinism():
    r = _row(table_name="Orders_Old",
             overall_earliest_date=_years_ago(16), overall_latest_date=_years_ago(16),
             years_since_latest_date=16.0, total_rows=50000, reserved_mb=150.0)
    assert classify_table(r, NOW) == classify_table(r, NOW)


# ── Never uses the word "unused" in any reason ───────────────────────────────
def test_no_unused_wording():
    cases = [
        _row(table_name="Orders_Old", overall_earliest_date=_years_ago(16),
             overall_latest_date=_years_ago(16), years_since_latest_date=16.0,
             total_rows=50000, reserved_mb=150.0),
        _row(table_name="LookupCodes", total_rows=5000, reserved_mb=200.0),
        _row(table_name="Broken", failed=True, error_message="boom"),
    ]
    for r in cases:
        assert "unused" not in classify_table(r, NOW)["reason"].lower()


def _run_all():
    tests = sorted(name for name in globals() if name.startswith("test_"))
    passed = 0
    for name in tests:
        globals()[name]()
        print(f"  PASS  {name}")
        passed += 1
    print(f"\n{passed}/{len(tests)} tests passed.")


if __name__ == "__main__":
    _run_all()
