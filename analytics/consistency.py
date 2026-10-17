"""
Shared consistency-score calculation.

The Phase 6 consistency score is the inverse of the coefficient of variation
of per-position return ratios (rho = net_result / cost), scaled to 0-10:

    mean == 0  -> 0.0
    otherwise  -> clamp(10 / (1 + |stdev / mean|), 0, 10)

This formula previously appeared inline in three places (the hard filter, the
ranking score, and the scan diagnostics). It is centralised here so a FAIL
record's persisted ``consistency_score`` is provably the same number the
filter used. Every existing call site keeps its own rounding and its own
treatment of a missing dispersion history, so no decision changes:

* ``filters/wallet_filter.py`` -- unrounded, compared with MIN_CONSISTENCY;
* ``wallet/ranking.py``        -- unrounded, feeds the score;
* ``monitor/scan_report.py``   -- rounded to 2dp for display, and still
  reported as None (not 0.0) when the mean return is zero.

The helper deliberately returns ``None`` when the dispersion inputs are
missing (fewer than three eligible positions): "not computable" must never be
written as a misleading zero.
"""
from __future__ import annotations

from typing import Optional


def consistency_score_from_dispersion(
    mean_roi: Optional[float],
    stdev_roi: Optional[float],
) -> Optional[float]:
    """
    Consistency score in [0, 10] from the mean/stdev of per-position rho.

    Returns None when either input is missing (dispersion unavailable), 0.0
    when the mean return is exactly zero, and the clamped inverse-CV score
    otherwise. The arithmetic is identical to the inline expressions this
    helper replaced.
    """
    if mean_roi is None or stdev_roi is None:
        return None
    if mean_roi == 0:
        return 0.0
    cv = abs(stdev_roi / mean_roi)
    return max(0.0, min(10.0, 10.0 / (1.0 + cv)))
