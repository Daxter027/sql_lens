"""
Unit tests for analysis/index_fragmentation.py pure decision helpers.

No DB and no pytest required. Run directly:

    python backend/tests/test_index_fragmentation.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import (  # noqa: E402
    INDEX_FRAG_REORG_THRESHOLD, INDEX_FRAG_REBUILD_THRESHOLD, INDEX_FRAG_MIN_PAGES,
)
from analysis.index_fragmentation import (  # noqa: E402
    recommend_op, is_candidate, severity_for,
)


def test_recommend_op_boundary():
    # REORGANIZE below the rebuild threshold, REBUILD at/above it.
    assert recommend_op(INDEX_FRAG_REBUILD_THRESHOLD - 0.01) == "REORGANIZE"
    assert recommend_op(INDEX_FRAG_REBUILD_THRESHOLD) == "REBUILD"
    assert recommend_op(INDEX_FRAG_REBUILD_THRESHOLD + 50) == "REBUILD"
    assert recommend_op(INDEX_FRAG_REORG_THRESHOLD) == "REORGANIZE"


def test_is_candidate_thresholds():
    big = INDEX_FRAG_MIN_PAGES
    small = INDEX_FRAG_MIN_PAGES - 1
    # Fragmented enough AND big enough → candidate.
    assert is_candidate(INDEX_FRAG_REORG_THRESHOLD, big) is True
    assert is_candidate(35.0, big) is True
    # Below fragmentation threshold → not a candidate even if big.
    assert is_candidate(INDEX_FRAG_REORG_THRESHOLD - 0.01, big) is False
    # Big fragmentation but too small → not a candidate (noise).
    assert is_candidate(80.0, small) is False
    # Nulls are never candidates.
    assert is_candidate(None, big) is False
    assert is_candidate(50.0, None) is False


def test_severity_levels():
    # No rebuilds, low frag → Low.
    assert severity_for(0, INDEX_FRAG_REORG_THRESHOLD - 1) == "Low"
    # Any reorg-level frag → Medium.
    assert severity_for(0, INDEX_FRAG_REORG_THRESHOLD) == "Medium"
    # A rebuild candidate → Medium.
    assert severity_for(1, 35.0) == "Medium"
    # Many rebuilds → High.
    assert severity_for(11, 35.0) == "High"
    # Very high worst-case fragmentation → High.
    assert severity_for(0, 50.0) == "High"


def _run_all():
    tests = sorted(n for n in globals() if n.startswith("test_"))
    for n in tests:
        globals()[n]()
        print(f"  PASS  {n}")
    print(f"\n{len(tests)}/{len(tests)} tests passed.")


if __name__ == "__main__":
    _run_all()
