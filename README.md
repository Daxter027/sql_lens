# SQL Server Storage Optimization Tool

A read-only analysis + safe-remediation agent for SQL Server databases. A FastAPI
backend (`backend/`) runs deterministic checks against a connected database; a
React/Vite frontend (`frontend/`) renders them as a tile grid with per-issue
detail modals.

## Running locally

```bash
# Backend (Python 3.10+, pyodbc + ODBC Driver for SQL Server)
cd backend
uvicorn main:app --reload          # http://127.0.0.1:8000

# Frontend (Node 18+)
cd frontend
npm install
npm run dev                        # http://localhost:5173  (proxies /api → backend)
```

Tests (no pytest required — each file is runnable directly):

```bash
cd backend
python tests/test_index_fragmentation.py
python tests/test_data_file_reclaim.py
python tests/test_archival_candidates.py
python tests/test_storage_redundancy.py
```

---

## AI Storage & Redundancy Analysis (Anthropic Claude API)

One feature — **"AI Storage & Redundancy Analysis"** — uses the **Anthropic
[Claude API](https://www.anthropic.com)**. It finds the largest ~20% of tables by
storage and has Claude produce a short, fixed-template Markdown report. It is
**on-demand** (a "Run" button inside its tile's modal), not part of the automatic
analysis batch, because it makes a network call.

> **Privacy note:** this sends table **names, row counts and sizes** (never row
> contents) to Anthropic's cloud API. Earlier versions used a local Ollama model
> that kept everything on-machine; this no longer does.

### Requirements — an Anthropic API key

This feature will not work until an API key is configured on the **backend**.

1. **Get a key** — https://console.anthropic.com (`sk-ant-…`).
2. **Set it as an environment variable** (the backend reads it at startup; it is
   never sent to the browser and never logged):
   ```powershell
   # PowerShell — persists for future processes (then restart uvicorn):
   setx ANTHROPIC_API_KEY "sk-ant-..."
   # or for the current shell only:
   $env:ANTHROPIC_API_KEY = "sk-ant-..."
   ```
3. **Restart the backend** so it picks up the key.

A run typically completes in **a few seconds**. The report is intentionally
surface-level (naming patterns, near-identical row counts, size tiering) — the
prompt uses a fixed checklist + template for consistent output.

### Configuration

Set in `backend/config.py`, each overridable by an environment variable:

| Env var | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | _(empty)_ | **Required.** API key — env only, never hard-code |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` | Model id (e.g. `claude-haiku-4-5`) |
| `ANTHROPIC_BASE_URL` | `https://api.anthropic.com` | API endpoint |
| `ANTHROPIC_VERSION` | `2023-06-01` | `anthropic-version` header |
| `ANTHROPIC_TIMEOUT_SECONDS` | `120` | HTTP timeout |
| `ANTHROPIC_MAX_TOKENS` | `4000` | Max output tokens (raise if a report is clipped) |
| `ANTHROPIC_TEMPERATURE` | `0.25` | Low — consistency over creativity |
| `STORAGE_REDUNDANCY_ROW_CAP` | `200` | Max rows sent to the model |

The model can also be overridden **per request** from the UI dropdown (or
`POST /api/storage-redundancy?model=claude-haiku-4-5`) without a restart.

Example (PowerShell): `$env:ANTHROPIC_MODEL = "claude-haiku-4-5"; uvicorn main:app --reload`

### How it works (single function)

The entire feature — the SQL query for the top-20% tables **and** the Claude API
call — is one function: `run_storage_redundancy_analysis(conn)` in
`backend/analysis/storage_redundancy.py`, exposed via `POST /api/storage-redundancy`.
The SQL step and the model step run sequentially in that one body; only the
combined result (table data + model markdown + summary) is returned. If the SQL
step fails, the model is never called. The Anthropic Messages API is called with
stdlib `urllib` (no SDK dependency).

Errors surface as one specific state in the UI: **API key problem** (missing/
invalid key), **Anthropic API unreachable** (no network), **model not found**,
**rate limited**, **timeout**, **API error**, **database error**, or **empty
database**.
