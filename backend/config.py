"""
config.py
---------
All configurable thresholds and constants for the Storage Optimization Tool.
Edit values here to tune behaviour without touching business logic.

Environment overrides: any value read via os.getenv() below can be set in a
`.env` file next to this module (see `.env.example` for the full list). The tiny
loader below reads that file at import time. REAL environment variables always
win — a var already set in the OS environment is never overwritten by `.env`.
"""

import os


def _load_dotenv(path: str) -> None:
    """
    Minimal .env loader — no third-party dependency (matches the codebase's
    stdlib-only style). Parses `KEY=VALUE` lines; ignores blanks and `#`
    comments; strips optional surrounding single/double quotes. Does NOT
    overwrite variables already present in the OS environment, so an explicit
    `setx` / `$env:` / shell export takes precedence over the file.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                # Strip one layer of matching quotes, if any.
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                if key and key not in os.environ:
                    os.environ[key] = val
    except FileNotFoundError:
        pass  # No .env file — fall back to OS env / hard-coded defaults.


# Load backend/.env (alongside this file) before any os.getenv() calls below.
_load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# ─────────────────────────────────────────────────────────────────────────────
# Transaction Log thresholds
# ─────────────────────────────────────────────────────────────────────────────

# Maximum age (in minutes) of the most recent log backup for the fix to be
# considered safe. If the last log backup is older than this, the fix is
# blocked with a clear message.
#
# IMPORTANT: This org runs FULL database backups every hour, but full backups
# and transaction log backups are DIFFERENT things. A full backup does NOT
# truncate the log in FULL recovery model — only a log backup does.
# Do NOT set this to match the full backup cadence without verifying that
# log backups are actually running on a separate schedule.
LOG_BACKUP_THRESHOLD_MINUTES: int = 60

# After shrinking, how many times the current used space to keep as headroom.
# e.g. if log is currently using 400 MB, shrink target = max(400 * 2, floor)
SHRINK_HEADROOM_MULTIPLIER: float = 2.0

# Minimum size (MB) to ever shrink a log file to, regardless of current usage.
# Prevents shrinking to an unrealistically tiny size that would just auto-grow again.
SHRINK_FLOOR_MB: int = 512

# VLF count above which log fragmentation is flagged
VLF_HIGH_COUNT_THRESHOLD: int = 50

# Log size relative to data size — flag if log_size > data_size * this ratio
LOG_TO_DATA_SIZE_RATIO_THRESHOLD: float = 0.5

# ─────────────────────────────────────────────────────────────────────────────
# Heap clustering thresholds
# ─────────────────────────────────────────────────────────────────────────────

# Only flag heaps with at least this many rows
HEAP_MIN_ROW_COUNT: int = 10_000

# ─────────────────────────────────────────────────────────────────────────────
# String storage thresholds
# ─────────────────────────────────────────────────────────────────────────────

# When analysing a table, sample at most this many rows to avoid full scans
# on very large tables. The result notes whether it was sampled or a full scan.
# 2,000 is plenty to spot grossly over-declared columns (ratio >= 3x) and is
# ~5x cheaper to scan than 10,000 — the dominant cost of this check.
STRING_SAMPLE_ROWS: int = 2_000

# Skip tables with fewer than this many rows. Tiny tables hold negligible wasted
# space, and on a wide schema (esp. a remote server) the per-table round-trips
# dominate — this keeps the check from sampling hundreds of trivial tables.
STRING_MIN_TABLE_ROWS: int = 1_000

# Flag a column if declared length is this many times larger than observed max
STRING_OVERSIZE_RATIO: float = 3.0

# ─────────────────────────────────────────────────────────────────────────────
# Unused index thresholds
# ─────────────────────────────────────────────────────────────────────────────

# If the SQL Server instance restarted less than this many days ago,
# usage stats are too young to be reliable — mark confidence as LOW.
UNUSED_INDEX_MIN_DAYS_SINCE_RESTART: int = 7

# An index is flagged only when it has ZERO reads AND at least this many writes
# (user_updates) since the last SQL Server restart. Writes are the cost of an
# index (every INSERT/UPDATE/DELETE must maintain it), so this is the minimum
# write overhead an unused index must be causing before it's worth flagging.
# Lower = more (noisier) candidates; higher = fewer, higher-overhead ones.
# 500 is a balanced default: real, repeated write work without flagging idle indexes.
UNUSED_INDEX_MIN_WRITES: int = 500

# ─────────────────────────────────────────────────────────────────────────────
# Ghost pages thresholds
# ─────────────────────────────────────────────────────────────────────────────

# Minimum ghost record count to flag a table/index
GHOST_RECORD_MIN_COUNT: int = 1_000

# Only physically scan (SAMPLED) tables whose total size is at least this many
# 8 KB pages. Ghost detection requires reading leaf pages, which is expensive;
# scanning the WHOLE database is far too slow on large servers (minutes). A table
# big enough to hold a meaningful ghost backlog clears this bar easily.
# 1,000 pages ≈ 8 MB. Tables below it are skipped (noted in the result).
GHOST_MIN_PAGES: int = 1_000

# ─────────────────────────────────────────────────────────────────────────────
# Index fragmentation thresholds
# ─────────────────────────────────────────────────────────────────────────────

# Fragmentation % at/above which a rowstore index is flagged. Below the REORG
# threshold, fragmentation is negligible and any maintenance just wastes I/O and
# transaction log. Microsoft's long-standing guidance: ignore < 10%,
# REORGANIZE 10–30%, REBUILD >= 30%.
INDEX_FRAG_REORG_THRESHOLD: float = 10.0
INDEX_FRAG_REBUILD_THRESHOLD: float = 30.0

# Ignore small indexes below this many 8 KB pages (~8 MB). Fragmentation in tiny
# indexes is noise — they often share mixed extents and rebuilding them yields
# nothing measurable while still generating log. 1,000 pages ≈ 8 MB.
INDEX_FRAG_MIN_PAGES: int = 1_000

# ─────────────────────────────────────────────────────────────────────────────
# Data-file space reclamation
# ─────────────────────────────────────────────────────────────────────────────

# Target leaves a protective free-space cushion so normal traffic doesn't
# immediately trigger expensive OS-level auto-growths after a shrink:
#   Target File Size (MB) = Used Space (MB) / DATA_FILE_BUFFER_RATIO
# 0.84 → ~16% free headroom retained.
DATA_FILE_BUFFER_RATIO: float = 0.84

# Don't bother flagging / shrinking a data file unless at least this much space
# is reclaimable past the buffer — tiny reclamations aren't worth the I/O.
DATA_FILE_RECLAIM_MIN_MB: int = 100

# Lock-timeout (ms) applied to DBCC SHRINKFILE so it BACKS OFF on a blocking
# lock instead of waiting indefinitely (or killing the blocker). On timeout the
# blocking SPID is reported and the operation aborts gracefully.
SHRINK_LOCK_TIMEOUT_MS: int = 30_000

# ─────────────────────────────────────────────────────────────────────────────
# Storage & Redundancy Analysis (Anthropic Claude API)
# ─────────────────────────────────────────────────────────────────────────────
# Constants live here like everything else, but each accepts an environment
# override so the model/key/endpoint can be swapped at runtime without a code
# change (the rest of the codebase has no env-var usage — this is the one feature
# that benefits from it, per its spec).
#
# PRIVACY NOTE: unlike the previous local-Ollama path, this sends table names,
# row counts and sizes to Anthropic's cloud API. The DATA never includes row
# contents — only schema/size metadata from sys.dm_db_partition_stats.

# API key — read from the environment ONLY. NEVER hard-code a key here, NEVER log
# it, and NEVER send it to the frontend. Empty by default; the feature returns a
# clear "auth_error" until it is set.
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")

# Anthropic Messages API endpoint + version header.
ANTHROPIC_BASE_URL: str = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
ANTHROPIC_VERSION: str = os.getenv("ANTHROPIC_VERSION", "2023-06-01")

# Default model. Sonnet balances quality/cost well for this short report; swap to
# claude-haiku-4-5 (cheaper/faster) or claude-opus-4-8 (max quality) per run via
# the UI dropdown / ?model=, or change this default.
ANTHROPIC_MODEL: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

# Cloud inference is fast — a short timeout is fine (seconds).
ANTHROPIC_TIMEOUT_SECONDS: int = int(os.getenv("ANTHROPIC_TIMEOUT_SECONDS", "120"))

# Max output tokens. Big enough that the full fixed-template report is not
# clipped (a long table list needs well over 1500); the model stops on its own
# well before this. _call_claude flags the response if it ever hits this cap.
ANTHROPIC_MAX_TOKENS: int = int(os.getenv("ANTHROPIC_MAX_TOKENS", "4000"))
ANTHROPIC_TEMPERATURE: float = float(os.getenv("ANTHROPIC_TEMPERATURE", "0.25"))

# Cap rows sent to the model. Claude handles large context easily, so this is
# generous now (covers the top-20% of most databases without truncation).
STORAGE_REDUNDANCY_ROW_CAP: int = int(os.getenv("STORAGE_REDUNDANCY_ROW_CAP", "200"))

# ─────────────────────────────────────────────────────────────────────────────
# Connection / query execution
# ─────────────────────────────────────────────────────────────────────────────

# Per-query timeout (seconds). A safety net so a single pathological query can
# never hang a request indefinitely; the affected check fails gracefully instead.
# 0 disables the timeout. Generous by design — normal checks finish well under it.
QUERY_TIMEOUT_SECONDS: int = 180

# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — Problem 14: Blank-string contamination
# ─────────────────────────────────────────────────────────────────────────────

# Cap on how many NOT NULL text columns to scan per run (prioritised by rows).
BLANK_STRING_MAX_COLUMNS_TO_SCAN: int = 20

# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — Problem 20: Shadow / twin tables
# ─────────────────────────────────────────────────────────────────────────────

# Name fragments that mark a table as a possible obsolete copy/backup/temp.
SHADOW_TABLE_NAME_PATTERNS: list[str] = [
    "backup", "bkp", "copy", "old", "temp", "test", "archive",
]
# Suffix patterns stripped to guess an active "counterpart" table (heuristic).
SHADOW_TABLE_SUFFIX_HINTS: list[str] = [
    "_old", "_temp", "_tmp", "_backup", "_bkp", "_copy", "_archive", "_test",
]
# Suffix appended on quarantine rename (date is filled in at execution time).
SHADOW_QUARANTINE_SUFFIX: str = "_QUARANTINED_"

# Cap on how many candidates to deep-analyze (dependency scan) per run, by size.
SHADOW_MAX_CANDIDATES: int = 100

# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — Problem 24: Inappropriate datatypes (FLOAT/REAL for identifiers)
# ─────────────────────────────────────────────────────────────────────────────

# Cap on how many FLOAT/REAL columns to sample per run (prioritised by rows).
INAPPROPRIATE_DT_MAX_COLUMNS_TO_SCAN: int = 40
# Rows to sample per column when checking for non-whole-number values.
INAPPROPRIATE_DT_SAMPLE_ROWS: int = 5_000

# ─────────────────────────────────────────────────────────────────────────────
# Session management
# ─────────────────────────────────────────────────────────────────────────────

# Session lifetime in seconds (4 hours)
SESSION_TTL_SECONDS: int = 4 * 60 * 60
