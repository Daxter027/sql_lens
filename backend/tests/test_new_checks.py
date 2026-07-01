"""
Unit tests for the pure helpers of the new analysis modules
(missing_indexes, duplicate_indexes, security_audit).

No DB / no pytest:

    python backend/tests/test_new_checks.py
"""

from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from analysis.missing_indexes import _clean_cols, _build_create_index          # noqa: E402
from analysis.duplicate_indexes import _keeper, _finding                       # noqa: E402
from analysis.security_audit import _tokens, _PII                              # noqa: E402


# ── missing_indexes: column cleanup + CREATE script ──────────────────────────
def test_clean_cols():
    assert _clean_cols("[A], [B]") == "A, B"
    assert _clean_cols(None) == ""


def test_build_create_index():
    sql = _build_create_index("dbo", "Orders", "[CustomerID]", "[OrderDate]", "[Total]")
    assert sql.startswith("CREATE NONCLUSTERED INDEX")
    assert "ON [dbo].[Orders]" in sql
    assert "([CustomerID], [OrderDate])" in sql
    assert "INCLUDE ([Total])" in sql
    # no INCLUDE clause when there are no included columns
    assert "INCLUDE" not in _build_create_index("dbo", "T", "[A]", None, None)


# ── duplicate_indexes: keeper prefers constraint/clustered indexes ───────────
def _idx(name, **kw):
    base = {"object_id": 1, "index_id": 2, "schema": "dbo", "table": "T", "name": name,
            "type_desc": "NONCLUSTERED", "is_pk": False, "is_uc": False, "is_unique": False,
            "keys": ["Code"], "includes": set()}
    base.update(kw)
    return base


def test_keeper_prefers_pk():
    plain = _idx("ix_code")
    pk = _idx("PK_T", is_pk=True, type_desc="CLUSTERED", index_id=1)
    assert _keeper(plain, pk) is pk
    assert _keeper(pk, plain) is pk


def test_finding_shape():
    drop = _idx("ix_dup", index_id=3)
    keep = _idx("PK_T", is_pk=True)
    f = _finding(drop, keep, "Exact duplicate", {(1, 3): 12.5})
    assert f["index"] == "ix_dup" and f["redundant_with"] == "PK_T"
    assert f["kind"] == "Exact duplicate" and f["size_mb"] == 12.5
    assert f["drop_script"] == "DROP INDEX [ix_dup] ON [dbo].[T];"


# ── security_audit: tokeniser + PII matching (must catch, must not over-match) ─
def test_tokens_camel_and_separators():
    assert _tokens("PANNo") == {"pan", "no"}
    assert _tokens("student_aadhaar_number") == {"student", "aadhaar", "number"}
    assert _tokens("EmailID") == {"email", "id"}


def _match(colname):
    toks = _tokens(colname)
    for cat, pred in _PII:
        if pred(toks):
            return cat
    return None


def test_pii_positive():
    assert _match("AadhaarNo") == "Aadhaar"
    assert _match("pan_card") == "PAN"
    assert _match("student_email") == "Email"
    assert _match("MobileNumber") == "Phone"
    assert _match("Password") == "Password"
    assert _match("Card_No") == "Card"
    assert _match("cvv") == "CVV"


def test_pii_no_false_positives():
    # 'panel'/'company' must NOT trigger PAN; 'card' alone (no no/number) must not
    assert _match("panel_name") is None
    assert _match("company_code") is None
    assert _match("card_type") is None       # 'card' without no/number
    assert _match("description") is None


def _run_all():
    tests = sorted(n for n in globals() if n.startswith("test_"))
    for n in tests:
        globals()[n]()
        print(f"  PASS  {n}")
    print(f"\n{len(tests)}/{len(tests)} tests passed.")


if __name__ == "__main__":
    _run_all()
