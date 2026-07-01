"""
Unit tests for analysis/data_file_reclaim.py pure helpers (no DB / no pytest).

    python backend/tests/test_data_file_reclaim.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from analysis.data_file_reclaim import target_mb, shrink_required  # noqa: E402


def test_target_leaves_16_percent():
    # Used / 0.84 leaves ~16% free, rounded up.
    assert target_mb(840) == 1000          # 840 / 0.84 = 1000 exactly
    assert target_mb(0) == 0
    # Rounds UP (ceil) so the target never sits below the true cushion.
    assert target_mb(841) == 1002          # 841/0.84 = 1001.19 -> 1002
    # Used itself is always < target (cushion exists).
    assert target_mb(5000) > 5000


def test_shrink_required_gate():
    # File far bigger than target with > min reclaim → required.
    assert shrink_required(2000, 1000, min_reclaim_mb=100) is True
    # Already tightly packed (size <= target) → not required.
    assert shrink_required(1000, 1000, min_reclaim_mb=100) is False
    assert shrink_required(900, 1000, min_reclaim_mb=100) is False
    # Excess exists but below the minimum-worthwhile threshold → not required.
    assert shrink_required(1050, 1000, min_reclaim_mb=100) is False
    assert shrink_required(1100, 1000, min_reclaim_mb=100) is True   # exactly 100


def test_realistic_example():
    # A 10 GB file using 5 GB: target ~5.95 GB, ~4 GB reclaimable → shrink required.
    used, size = 5000, 10000
    tgt = target_mb(used)
    assert tgt == 5953            # ceil(5000/0.84)
    assert shrink_required(size, tgt) is True


def _run_all():
    tests = sorted(n for n in globals() if n.startswith("test_"))
    for n in tests:
        globals()[n]()
        print(f"  PASS  {n}")
    print(f"\n{len(tests)}/{len(tests)} tests passed.")


if __name__ == "__main__":
    _run_all()
