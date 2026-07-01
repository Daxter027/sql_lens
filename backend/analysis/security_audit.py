"""
security_audit.py
-----------------
Read-only security posture audit + heuristic PII/sensitive-column discovery.

Covers:
  • Surface area   — risky instance features enabled (xp_cmdshell, OLE Automation,
                     CLR, Ad Hoc Distributed Queries, cross-db ownership chaining).
  • Encryption     — TDE (data-at-rest) on/off for this database.
  • Principals     — orphaned users, guest CONNECT, db_owner sprawl, extra sysadmins.
  • Sensitive data — columns whose NAME suggests PII (Aadhaar, PAN, email, phone,
                     password, card, DOB, …) — candidates for masking/encryption.

ANALYSIS-ONLY. It reports findings; it NEVER changes permissions, disables
features, or masks data — those are deliberate, high-blast-radius DBA actions.
Each sub-check degrades gracefully if the login lacks the permission to run it.
"""

from __future__ import annotations
import logging
import re
from typing import Any
import pyodbc

logger = logging.getLogger(__name__)

ISSUE_ID   = "security_audit"
ISSUE_NAME = "Security Posture & PII Audit"


def _finding(category, finding, detail, risk):
    return {"category": category, "finding": finding, "detail": detail, "risk": risk}


# ── surface-area features: name → (risk, friendly) ───────────────────────────
_SURFACE = {
    "xp_cmdshell":                 ("High",   "OS command execution from T-SQL"),
    "Ole Automation Procedures":   ("Medium", "COM/OLE automation from T-SQL"),
    "clr enabled":                 ("Medium", "CLR assembly execution"),
    "Ad Hoc Distributed Queries":  ("Medium", "OPENROWSET/OPENQUERY ad-hoc access"),
    "cross db ownership chaining": ("Medium", "Cross-DB ownership chaining"),
}


def _check_surface(cur, out):
    try:
        cur.execute("SELECT name, CAST(value_in_use AS int) FROM sys.configurations WHERE name IN ({})"
                    .format(",".join("?" * len(_SURFACE))), *list(_SURFACE.keys()))
        for name, val in cur.fetchall():
            if val == 1:
                risk, desc = _SURFACE[name]
                out.append(_finding("Surface area", f"'{name}' is enabled", desc + " — disable if unused.", risk))
    except pyodbc.Error:
        logger.info("security_audit: surface-area check skipped (no permission)")


def _check_tde(cur, out):
    try:
        cur.execute("SELECT is_encrypted FROM sys.databases WHERE database_id = DB_ID()")
        row = cur.fetchone()
        if row is not None and not row[0]:
            out.append(_finding("Encryption", "TDE (data-at-rest) is OFF",
                                "Database files/backups are not encrypted at rest.", "Medium"))
    except pyodbc.Error:
        pass


def _check_principals(cur, out):
    # Orphaned users (SID has no matching server login; excludes contained-db users).
    try:
        cur.execute("""
            SELECT dp.name, dp.type_desc
            FROM sys.database_principals dp
            LEFT JOIN sys.server_principals sp ON sp.sid = dp.sid
            WHERE dp.type IN ('S','U','G') AND dp.sid IS NOT NULL
              AND dp.principal_id > 4
              AND dp.name NOT IN ('dbo','guest','INFORMATION_SCHEMA','sys')
              AND dp.authentication_type <> 2
              AND sp.sid IS NULL
        """)
        for name, tdesc in cur.fetchall():
            out.append(_finding("Principals", f"Orphaned user '{name}'",
                                f"{tdesc} user with no matching server login.", "Medium"))
    except pyodbc.Error:
        pass

    # guest with CONNECT.
    try:
        cur.execute("""
            SELECT 1 FROM sys.database_permissions perm
            JOIN sys.database_principals dp ON dp.principal_id = perm.grantee_principal_id
            WHERE dp.name = 'guest' AND perm.permission_name = 'CONNECT' AND perm.state = 'G'
        """)
        if cur.fetchone():
            out.append(_finding("Principals", "guest account has CONNECT",
                                "The guest user can access this database — revoke unless required.", "Medium"))
    except pyodbc.Error:
        pass

    # db_owner members other than dbo.
    try:
        cur.execute("""
            SELECT m.name
            FROM sys.database_role_members rm
            JOIN sys.database_principals r ON r.principal_id = rm.role_principal_id AND r.name = 'db_owner'
            JOIN sys.database_principals m ON m.principal_id = rm.member_principal_id
            WHERE m.name <> 'dbo'
        """)
        members = [r[0] for r in cur.fetchall()]
        if members:
            out.append(_finding("Principals", f"{len(members)} extra db_owner member(s)",
                                "Full control of this DB: " + ", ".join(members[:10])
                                + ("…" if len(members) > 10 else ""), "High"))
    except pyodbc.Error:
        pass

    # Extra sysadmins (server-level — may not be visible to a low-priv login).
    try:
        cur.execute("""
            SELECT sp.name
            FROM sys.server_role_members srm
            JOIN sys.server_principals r  ON r.principal_id  = srm.role_principal_id AND r.name = 'sysadmin'
            JOIN sys.server_principals sp ON sp.principal_id = srm.member_principal_id
            WHERE sp.name NOT LIKE 'NT SERVICE%' AND sp.name NOT LIKE 'NT AUTHORITY%'
        """)
        admins = [r[0] for r in cur.fetchall()]
        if len(admins) > 3:
            out.append(_finding("Principals", f"{len(admins)} sysadmin logins",
                                "Many principals have full server control: "
                                + ", ".join(admins[:10]) + ("…" if len(admins) > 10 else ""), "Medium"))
    except pyodbc.Error:
        pass


# ── PII name matching ────────────────────────────────────────────────────────
def _tokens(name: str) -> set[str]:
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", name)   # acronym boundary: PANNo → PAN No
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)         # camelCase → words
    s = re.sub(r"[^A-Za-z0-9]+", " ", s)                  # separators → space
    return {t.lower() for t in s.split() if t}


# category → predicate over the column's token set
_PII = [
    ("Aadhaar",  lambda t: bool(t & {"aadhaar", "aadhar", "uidai"})),
    ("PAN",      lambda t: "pan" in t),
    ("Passport", lambda t: "passport" in t),
    ("Email",    lambda t: bool(t & {"email", "emailid", "mail"})),
    ("Phone",    lambda t: bool(t & {"phone", "mobile", "telephone"})),
    ("DOB",      lambda t: bool(t & {"dob", "dateofbirth"}) or ("birth" in t and bool(t & {"date", "dt"}))),
    ("Password", lambda t: bool(t & {"password", "passwd", "pwd"})),
    ("Card",     lambda t: "card" in t and bool(t & {"no", "number", "num", "cvv"})),
    ("Bank a/c", lambda t: "ifsc" in t or (bool(t & {"account", "acct"}) and bool(t & {"no", "number", "num"}))),
    ("CVV",      lambda t: "cvv" in t),
]
_PII_RISK = {"Password": "High", "Card": "High", "CVV": "High", "Aadhaar": "High", "PAN": "High"}


def _check_pii(cur, out):
    try:
        cur.execute("""
            SELECT s.name, t.name, c.name
            FROM sys.columns c
            JOIN sys.tables  t ON t.object_id = c.object_id
            JOIN sys.schemas s ON s.schema_id = t.schema_id
            WHERE t.is_ms_shipped = 0
        """)
    except pyodbc.Error:
        return
    by_cat: dict[str, list] = {}
    for schema, table, col in cur.fetchall():
        toks = _tokens(col)
        for cat, pred in _PII:
            if pred(toks):
                by_cat.setdefault(cat, []).append(f"{schema}.{table}.{col}")
                break
    for cat, cols in by_cat.items():
        risk = _PII_RISK.get(cat, "Medium")
        sample = ", ".join(cols[:8]) + ("…" if len(cols) > 8 else "")
        out.append(_finding("Sensitive data", f"{len(cols)} likely {cat} column(s)",
                            "Candidates for masking/encryption: " + sample, risk))


_RISK_RANK = {"High": 3, "Medium": 2, "Low": 1}


def analyze(conn: pyodbc.Connection) -> dict[str, Any]:
    cur = conn.cursor()
    findings: list[dict] = []
    _check_surface(cur, findings)
    _check_tde(cur, findings)
    _check_principals(cur, findings)
    _check_pii(cur, findings)

    findings.sort(key=lambda f: _RISK_RANK.get(f["risk"], 0), reverse=True)
    highs = sum(1 for f in findings if f["risk"] == "High")
    meds  = sum(1 for f in findings if f["risk"] == "Medium")
    note = ("Read-only audit — nothing was changed. PII detection is name-based and "
            "heuristic (verify before acting). Remediation (revoking permissions, disabling "
            "features, masking/encrypting columns) is a manual DBA decision.")

    if not findings:
        return {
            "issue_id": ISSUE_ID, "issue_name": ISSUE_NAME, "severity": "Low",
            "affected_objects": [], "current_metrics": {"finding_count": 0},
            "recommended_action": "No security-posture or PII findings surfaced.",
            "estimated_impact": "N/A", "executable": False, "eligible_for_fix": False,
            "analysis_note": note,
        }

    severity = "High" if highs else "Medium" if meds else "Low"
    return {
        "issue_id": ISSUE_ID, "issue_name": ISSUE_NAME, "severity": severity,
        "affected_objects": findings,
        "current_metrics": {
            "finding_count": len(findings), "high_risk": highs, "medium_risk": meds,
        },
        "recommended_action": (
            f"{len(findings)} finding(s) ({highs} high, {meds} medium). Review surface-area "
            "features, over-privileged principals, orphaned users, and the sensitive-column "
            "candidates — mask/encrypt PII and revoke unnecessary access as appropriate."
        ),
        "estimated_impact": "Reduced attack surface and clearer data-protection posture.",
        "executable": False, "eligible_for_fix": False,
        "analysis_note": note,
    }
