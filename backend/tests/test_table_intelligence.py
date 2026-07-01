"""
Unit tests for analysis/table_intelligence.py — the pure (no-DB) helpers.

No pytest required:

    python backend/tests/test_table_intelligence.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from analysis.table_intelligence import (  # noqa: E402
    _report_tokens, _match_reports, _row_to_dict, _INT_COLS,
)


# ── _report_tokens: prefers <CommandText>, else whole RDL, word-tokenised ─────
def test_report_tokens_commandtext():
    rdl = "<Report><Query><CommandText>SELECT * FROM dbo.Marks m JOIN Challans c</CommandText></Query></Report>"
    toks = _report_tokens(rdl)
    assert "marks" in toks and "challans" in toks
    assert "report" not in toks   # outside CommandText, ignored when CommandText exists


def test_report_tokens_fallback_whole_rdl():
    rdl = "<Report><Textbox>uses Marks table</Textbox></Report>"   # no CommandText
    toks = _report_tokens(rdl)
    assert "marks" in toks        # fell back to scanning the whole RDL


# ── _match_reports: word-boundary counting, no substring false positives ──────
def test_match_reports_counts_and_wordboundary():
    reports = [
        ("/r/one", "<CommandText>SELECT * FROM Marks</CommandText>"),
        ("/r/two", "<CommandText>SELECT * FROM Marks JOIN Challans</CommandText>"),
        ("/r/three", "<CommandText>SELECT * FROM Remarks</CommandText>"),  # must NOT match 'Marks'
        ("/r/four", None),   # null RDL is skipped safely
    ]
    res = _match_reports(reports, ["Marks", "Challans"])
    assert res["marks"]["count"] == 2, res["marks"]
    assert res["challans"]["count"] == 1
    assert sorted(res["marks"]["samples"]) == ["/r/one", "/r/two"]


def test_match_reports_sample_cap():
    reports = [(f"/r/{i}", "<CommandText>FROM Marks</CommandText>") for i in range(40)]
    res = _match_reports(reports, ["Marks"])
    assert res["marks"]["count"] == 40
    assert len(res["marks"]["samples"]) == 25   # _SAMPLE_CAP


# ── _row_to_dict: int/bool coercion + cold flag + SSRS defaults ───────────────
def _mk_row(cols, **over):
    base = {c: 0 for c in cols}
    base.update({"SchemaName": "dbo", "TableName": "T", "TotalMB": 1.5,
                 "LastWrite": None, "LastRead": None})
    base.update(over)
    return [base[c] for c in cols]


def test_row_to_dict_types_and_cold():
    cols = ["SchemaName", "TableName", "Created", "SchemaModified", "TotalMB",
            "LastWrite", "LastRead"] + list(_INT_COLS)
    # a table with zero reads/writes -> cold since restart
    row = _mk_row(cols, IsHeap=1, HasPK=0, Writes=0, Reads=0, RowCount="500")
    d = _row_to_dict(cols, row)
    assert d["IsHeap"] is True and d["HasPK"] is False
    assert d["RowCount"] == 500 and isinstance(d["RowCount"], int)
    assert d["TotalMB"] == 1.5 and isinstance(d["TotalMB"], float)
    assert d["ColdSinceRestart"] is True
    assert d["ReportCount"] == 0 and d["ReportSamples"] == []

    # any activity -> not cold
    row2 = _mk_row(cols, Writes=0, Reads=7)
    assert _row_to_dict(cols, row2)["ColdSinceRestart"] is False


def _run_all():
    tests = sorted(n for n in globals() if n.startswith("test_"))
    for n in tests:
        globals()[n]()
        print(f"  PASS  {n}")
    print(f"\n{len(tests)}/{len(tests)} tests passed.")


if __name__ == "__main__":
    _run_all()
