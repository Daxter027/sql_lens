# SQL Lens

A read-only diagnostic tool for SQL Server. It connects to a database, runs a set
of metadata- and DMV-based checks, and reports findings in a web UI. For any
change that carries risk, it generates a script for review rather than executing
it. A FastAPI service performs the analysis; a React/Vite single-page app renders
the results.

Tested against SQL Server 2008 through 2019 (Standard and Enterprise).

## Design principles

- **Read-only analysis.** Checks query system views and dynamic management views
  only. They do not read or modify user data.
- **Script generation over execution.** Index changes, compression, statistics
  updates, permission changes, and cache operations are emitted as T-SQL for the
  operator to run in a maintenance window. The tool does not apply them.
- **Safe, reversible remediation only** for the small number of actions it does
  perform (for example, disabling rather than dropping an unused index, or a
  targeted log-file shrink).
- **Credentials remain server-side.** The database password is used only to build
  a connection string and is held in an in-memory session. The client receives an
  opaque session token, never the password. Driver errors are sanitised before
  reaching the browser.

## Requirements

- Python 3.10 or later, with `pyodbc` and the Microsoft ODBC Driver for SQL Server
- Node.js 18 or later
- A SQL Server login with read access to the target database. Some checks also
  require `VIEW SERVER STATE`.

## Running

Backend:

```bash
cd backend
pip install -r ../requirements.txt
uvicorn main:app --reload          # http://127.0.0.1:8000
```

Frontend:

```bash
cd frontend
npm install
npm run dev                        # http://localhost:5173 (proxies /api to the backend)
```

Open the frontend, enter the server and database, choose Windows or SQL
authentication, and connect. Once connected, the header provides a dropdown to
switch to another database on the same server without re-entering credentials.

## Diagnostics

Most checks run automatically as a batch when analysis starts. Checks marked
on-demand are heavier and run only when opened, via a button in their panel.

### Storage

| Check | Description | Run |
|---|---|---|
| Transaction Log Growth | Reclaimable log space, VLF count, recovery model, log-backup age | Auto |
| Heap to Clustered Index | Heaps that would benefit from a clustered index | Auto |
| Unused Index Audit | High-write, zero-read indexes; remediation disables (does not drop) them | Auto |
| Ghost Pages | Ghost and forwarded record reconciliation | Auto |
| Index Fragmentation | Fragmented indexes with REORGANIZE/REBUILD recommendation | Auto |
| Data File Reclamation | Free space recoverable via TRUNCATEONLY or compaction | Auto |
| Data Compression Savings | ROW/PAGE savings estimated with `sp_estimate_data_compression_savings`, plus `ALTER ... REBUILD` scripts | On-demand |
| Duplicate and Overlapping Indexes | Exact-duplicate and prefix-overlap indexes, with DROP scripts | Auto |
| Legacy Table Archival Candidates | Deterministic scoring of cold or legacy tables for review | Auto |
| Structural Twin and Shadow Tables | Backup-style copies; remediation is a reversible rename | Auto |

### Performance

| Check | Description | Run |
|---|---|---|
| Missing Index Recommendations | Suggestions from `sys.dm_db_missing_index_*`, ranked, with CREATE INDEX scripts | Auto |
| Stale Statistics | Statistics stale by age or modification count, with UPDATE STATISTICS scripts. Falls back to a 2008-compatible query where `sys.dm_db_stats_properties` is unavailable | Auto |
| Ad-Hoc Workload and Plan Cache | Single-use plan-cache bloat, with configuration and cache-flush scripts | Auto |

### Security

| Check | Description | Run |
|---|---|---|
| Security Posture and PII Audit | Surface-area configuration, TDE status, orphaned users, `db_owner`/sysadmin membership, and heuristic detection of columns likely to hold PII | Auto |

### Data quality and reporting

| Check | Description | Run |
|---|---|---|
| Table Intelligence | Per-table profile: size, age, dependency count, indexes/triggers/foreign keys, activity, and SSRS report usage | On-demand |
| AI Storage and Redundancy | Summarises the largest tables for naming and redundancy patterns using the Anthropic API | On-demand |
| String Storage and Data Types | Oversized string columns and inappropriate data types | Auto |
| Blank-String Contamination | Empty-string versus NULL contamination | Auto |

Result tables can be copied to the clipboard or downloaded as an `.xlsx` file.
On-demand results are cached while the session lasts and cleared when the
database is switched or the connection is closed.

## Configuration

Settings are defined in `backend/config.py`. Several accept environment
overrides. The backend loads `backend/.env` at startup if present; copy
`backend/.env.example` to `backend/.env` to use it. Environment variables set in
the operating system take precedence over values in `.env`.

The AI Storage and Redundancy check is the only feature that makes an external
network call. It sends table names, row counts, and sizes — not row contents — to
the Anthropic API. All other checks operate entirely against the SQL Server
instance.

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | (empty) | Required for the AI check only. Obtained from the Anthropic console. |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` | Model identifier; also selectable per run in the UI |
| `ANTHROPIC_MAX_TOKENS` | `4000` | Maximum report length |
| `ANTHROPIC_TIMEOUT_SECONDS` | `120` | HTTP timeout |
| `STORAGE_REDUNDANCY_ROW_CAP` | `200` | Maximum number of tables sent to the model |

If no key is configured, only the AI check is affected; it reports a clear error
state and every other check continues to work.

## Testing

Tests are self-contained Python scripts and do not require pytest:

```bash
cd backend
python tests/test_new_checks.py
python tests/test_table_intelligence.py
python tests/test_storage_redundancy.py
python tests/test_index_fragmentation.py
python tests/test_data_file_reclaim.py
python tests/test_archival_candidates.py
```

## Project structure

```
backend/
  main.py            FastAPI application and router registration
  config.py          thresholds and the .env loader
  db.py              pyodbc connection factory with sanitised errors
  session.py         in-memory session store keyed by opaque token
  analysis/          one module per check, each exposing analyze(conn)
  routers/           connect, analyze, execute, report, and on-demand endpoints
  tests/             runnable unit tests
frontend/
  src/components/    tile grid, detail modal, per-check panels, export controls
  src/api.js         fetch wrappers for the backend API
```

## Security considerations

- The database password is never logged, never returned to the client, and is
  used only to construct the connection string.
- The tool does not drop tables, does not terminate arbitrary sessions, and does
  not run the higher-risk remediation scripts on the operator's behalf.
- `backend/.env` (which holds the API key) and SQL Server data files
  (`*.mdf`, `*.ldf`, `*.bak`) are excluded from version control via `.gitignore`.

## Technology

Backend: FastAPI, pyodbc, Pydantic, standard-library `urllib` (no third-party SDK
for the API call), and a thread pool for parallel checks.

Frontend: React 18, Vite, SheetJS (loaded on demand for exports), and
react-markdown.
