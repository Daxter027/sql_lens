"""
models.py
---------
Pydantic request/response schemas for the Storage Optimization Tool API.
"""

from __future__ import annotations
from typing import Any, Optional
from pydantic import BaseModel, Field


# ─────────────────────────────────────────────────────────────────────────────
# Connect
# ─────────────────────────────────────────────────────────────────────────────

class ConnectRequest(BaseModel):
    server: str = Field(..., description="SQL Server host name or IP[:port]")
    database: str = Field(..., description="Target database name")
    auth_type: str = Field(..., description='"windows" or "sql"')
    username: Optional[str] = Field(None, description="SQL Auth username (sql auth only)")
    password: Optional[str] = Field(None, description="SQL Auth password (sql auth only) — never echoed back")
    trust_server_certificate: bool = Field(False, description="Trust self-signed server certificate")


class ConnectResponse(BaseModel):
    session_token: str
    server: str
    database: str
    message: str = "Connected successfully"


# ─────────────────────────────────────────────────────────────────────────────
# Analysis
# ─────────────────────────────────────────────────────────────────────────────

class IssueResult(BaseModel):
    issue_id: str
    issue_name: str
    severity: str                          # Low | Medium | High | None
    affected_objects: list[dict[str, Any]]
    current_metrics: dict[str, Any]
    recommended_action: str
    estimated_impact: str
    executable: bool
    eligible_for_fix: bool
    blocking_reason: Optional[str] = None  # Why fix can't run right now
    analysis_note: Optional[str] = None    # e.g. "sampled 10k rows"
    error: Optional[str] = None            # If the check itself errored
    recovery_decision_required: Optional[bool] = False
    explanation: Optional[str] = None
    options: Optional[list[dict[str, str]]] = None


class AnalyzeResponse(BaseModel):
    session_token: str
    database: str
    issues: list[IssueResult]
    analysed_at: str                       # ISO timestamp


# ─────────────────────────────────────────────────────────────────────────────
# Execute
# ─────────────────────────────────────────────────────────────────────────────

class ExecuteRequest(BaseModel):
    session_token: str
    issue_id: str
    recovery_choice: Optional[str] = None
    # Issue 2 (heap clustering): caller may pass target table/schema/key.
    # If omitted the backend auto-selects the first candidate from analysis.
    target_schema: Optional[str] = None
    target_table:  Optional[str] = None
    target_column: Optional[str] = None


class MetricSnapshot(BaseModel):
    """Typed snapshot kept for the transaction-log execute path only."""
    log_size_mb: float
    log_used_mb: float
    log_used_pct: float
    vlf_count: int


class ExecuteResponse(BaseModel):
    issue_id: str
    status: str                              # success | failed | skipped | partial
    command_executed: Optional[str] = None   # Primary audit command (single-op issues)
    # Generalised before/after metrics — each issue returns its own shape.
    # Transaction log path still populates these as MetricSnapshot-compatible dicts.
    before_metrics: Optional[dict[str, Any]] = None
    after_metrics:  Optional[dict[str, Any]] = None
    delta_mb_freed: Optional[float] = None   # Transaction log only
    # Multi-object outcomes (unused indexes, ghost pages): one entry per object.
    results: Optional[list[dict[str, Any]]] = None
    message: str
    executed_at: str
    recovery_choice: Optional[str] = None
    # data_file_reclaim: signals the UI to offer the Deep Compaction step, and
    # carries the post-shrink index-rebuild summary when deep compaction ran.
    deep_compaction_available: Optional[bool] = None
    rebuild_summary: Optional[dict[str, Any]] = None


# ─────────────────────────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────────────────────────

class ReportResponse(BaseModel):
    database: str
    server: str
    generated_at: str
    analysis: Optional[AnalyzeResponse] = None
    execution: Optional[ExecuteResponse] = None       # most recent (back-compat)
    executions: list[ExecuteResponse] = []            # every optimization run this session
    unexecuted_issues: list[IssueResult] = []  # Issues 2-5 findings for full picture


# ─────────────────────────────────────────────────────────────────────────────
# Storage & Redundancy Analysis (local Ollama model)
# ─────────────────────────────────────────────────────────────────────────────

class StorageRedundancyResponse(BaseModel):
    status: str                              # ok | empty | error
    total_user_table_count: int
    analyzed_table_count: int
    analyzed_percentage: float
    was_truncated: bool
    table_data: list[dict[str, Any]] = []
    analysis_markdown: Optional[str] = None
    model_used: Optional[str] = None
    error: Optional[str] = None
    error_kind: Optional[str] = None         # db_error | ollama_unreachable | model_not_found | timeout | ollama_error
    message: Optional[str] = None


class DataCompressionResponse(BaseModel):
    status: str                              # ok | empty | error
    mode: str = "PAGE"
    analyzed_table_count: int = 0
    tables: list[dict[str, Any]] = []
    total_current_mb: float = 0
    total_compressed_mb: float = 0
    total_savings_mb: float = 0
    total_savings_pct: float = 0
    error: Optional[str] = None
    error_kind: Optional[str] = None
    message: Optional[str] = None


class TableIntelligenceResponse(BaseModel):
    status: str                              # ok | empty | error
    total_tables: int = 0
    server_start_time: Optional[str] = None  # activity stats are only valid since this
    ssrs_available: bool = False
    ssrs_report_count: int = 0
    ssrs_note: Optional[str] = None
    tables: list[dict[str, Any]] = []        # one profile dict per user table
    error: Optional[str] = None
    error_kind: Optional[str] = None         # db_error
    message: Optional[str] = None
