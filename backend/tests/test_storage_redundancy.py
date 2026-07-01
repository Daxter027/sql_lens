"""
Unit tests for analysis/storage_redundancy.py — the single-function feature.

No DB and no pytest required (a tiny stub connection + a mock AI callable):

    python backend/tests/test_storage_redundancy.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pyodbc  # noqa: E402  (only its error type is referenced)
from analysis.storage_redundancy import (  # noqa: E402
    top_n, run_storage_redundancy_analysis, _format_rows,
    ApiUnreachable, ApiAuthError, ModelNotFound, ApiRateLimited, ApiTimeout,
)

_COLS = ["TableName", "SchemaName", "RowCount", "TotalSpaceMB", "UsedSpaceMB", "UnusedSpaceMB"]


class _StubCursor:
    """Returns `count` for the COUNT query and `rows` for the storage query.
    Raises pyodbc.Error from whichever query whose SQL contains `fail_on`."""
    def __init__(self, count, rows, fail_on=None):
        self._count, self._rows, self._fail_on = count, rows, fail_on
        self._mode = None
        self.description = None

    def execute(self, sql, *params):
        if self._fail_on and self._fail_on in sql:
            raise pyodbc.Error("HY000", "simulated failure")
        if "COUNT(*)" in sql:
            self._mode = "count"
            self.description = None
        else:
            self._mode = "rows"
            self.description = [(c,) for c in _COLS]
        return self

    def fetchone(self):
        return (self._count,) if self._mode == "count" else None

    def fetchall(self):
        return [tuple(r) for r in self._rows]


class _StubConn:
    def __init__(self, count, rows, fail_on=None):
        self._cur = _StubCursor(count, rows, fail_on)

    def cursor(self):
        return self._cur


def _row(name, schema="dbo", rows=1000, total=100.0, used=60.0, unused=40.0):
    return (name, schema, rows, total, used, unused)


# ── top_n (CEILING(0.20*count), floor 1, cap 2000) ───────────────────────────
def test_top_n_cases():
    assert top_n(800) == 160
    assert top_n(803) == 161
    assert top_n(5) == 1
    assert top_n(0) == 0
    assert top_n(100_000) == 2000   # capped


# ── Empty DB short-circuits WITHOUT calling the API ──────────────────────────
def test_empty_db_no_api():
    calls = []
    def mock(*a, **k): calls.append(k); return "x"
    res = run_storage_redundancy_analysis(_StubConn(0, []), ai_call=mock)
    assert res["status"] == "empty"
    assert res["total_user_table_count"] == 0
    assert len(calls) == 0          # API never invoked
    assert res["analysis_markdown"] is None


# ── SQL failure (storage query) → API NEVER called ───────────────────────────
def test_sql_fail_no_api():
    calls = []
    def mock(*a, **k): calls.append(k); return "x"
    conn = _StubConn(50, [_row("A")], fail_on="dm_db_partition_stats")
    res = run_storage_redundancy_analysis(conn, ai_call=mock)
    assert res["status"] == "error"
    assert res["error_kind"] == "db_error"
    assert len(calls) == 0          # the key assertion: zero API calls


# ── Count-query failure → also no API, db_error ──────────────────────────────
def test_count_fail_no_api():
    calls = []
    def mock(*a, **k): calls.append(k); return "x"
    conn = _StubConn(50, [_row("A")], fail_on="COUNT(*)")
    res = run_storage_redundancy_analysis(conn, ai_call=mock)
    assert res["status"] == "error" and res["error_kind"] == "db_error"
    assert len(calls) == 0


# ── Happy path → correct combined shape ──────────────────────────────────────
def test_combined_shape():
    rows = [_row("Orders", total=500), _row("Orders_Old", total=300), _row("Audit", total=120)]
    captured = {}
    def mock(prompt, **k):
        captured["prompt"] = prompt; captured["model"] = k.get("model")
        return "## Largest Tables\nOrders — 500 MB"
    res = run_storage_redundancy_analysis(_StubConn(15, rows), model="claude-sonnet-4-6", ai_call=mock)
    assert res["status"] == "ok"
    assert res["total_user_table_count"] == 15
    assert res["analyzed_table_count"] == top_n(15) == 3
    assert res["analyzed_percentage"] == 20.0
    assert res["was_truncated"] is False
    assert len(res["table_data"]) == 3
    assert res["table_data"][0]["TableName"] == "Orders"
    assert res["model_used"] == "claude-sonnet-4-6"
    assert res["analysis_markdown"].startswith("## Largest Tables")
    # Prompt is tab-separated and carries the data + the fixed template headers.
    assert "Orders_Old" in captured["prompt"]
    assert "## Recommended Next Steps" in captured["prompt"]


# ── Truncation flag + prompt note when rows exceed the cap ───────────────────
def test_truncation():
    rows = [_row(f"T{i}", total=100 - i) for i in range(5)]
    captured = {}
    def mock(prompt, **k): captured["prompt"] = prompt; return "## Largest Tables\nok"
    res = run_storage_redundancy_analysis(_StubConn(25, rows), row_cap=2, ai_call=mock)
    assert res["was_truncated"] is True
    assert len(res["table_data"]) == 5          # full data returned to the UI
    assert "TRUNCATED" in captured["prompt"]     # model told it's a subset


# ── Each Claude API failure maps to its specific error_kind ──────────────────
def test_error_paths():
    rows = [_row("A")]
    def raiser(exc):
        def _f(*a, **k): raise exc
        return _f
    cases = {
        "auth_error":      ApiAuthError("no key"),
        "api_unreachable": ApiUnreachable("down"),
        "model_not_found": ModelNotFound("no model"),
        "rate_limited":    ApiRateLimited("429"),
        "timeout":         ApiTimeout("slow"),
    }
    for kind, exc in cases.items():
        res = run_storage_redundancy_analysis(_StubConn(10, rows), ai_call=raiser(exc))
        assert res["status"] == "error", kind
        assert res["error_kind"] == kind, (kind, res["error_kind"])
        # Table data is still returned so the UI can show the raw table on error.
        assert len(res["table_data"]) == 1
        assert res["analysis_markdown"] is None


# ── _format_rows: header + tab layout + cap ──────────────────────────────────
def test_format_rows():
    data = [{"TableName": "A", "SchemaName": "dbo", "RowCount": 5,
             "TotalSpaceMB": 1.0, "UsedSpaceMB": 0.5, "UnusedSpaceMB": 0.5}]
    text, trunc = _format_rows(data, cap=10)
    assert text.split("\n")[0].startswith("TableName\tSchemaName")
    assert "A\tdbo\t5\t1.0\t0.5\t0.5" in text
    assert trunc is False


def _run_all():
    tests = sorted(n for n in globals() if n.startswith("test_"))
    for n in tests:
        globals()[n]()
        print(f"  PASS  {n}")
    print(f"\n{len(tests)}/{len(tests)} tests passed.")


if __name__ == "__main__":
    _run_all()
