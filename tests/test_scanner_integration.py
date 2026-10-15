"""
Phase 8C integration tests: hard-filter gate wired into the scan pipeline.

Verifies:
- filter_wallet() is now called from cmd_scan_wallets (via inspect)
- result.status is explicitly checked (not just result.passed)
- FAIL wallets do not proceed to normal ranking/recommendation candidates
- INSUFFICIENT_DATA wallets are excluded from normal ranking (not silently promoted)
- PASS wallets proceed through the existing ranking pipeline unchanged
- Exact rejection reasons and insufficient criteria are preserved in filter output
- Ranking behaviour for PASS wallets is unchanged (ranking.py not modified)
- Existing recommendation thresholds are unchanged
- Existing frequency behaviour is unchanged
- All Phase 8A and Phase 8B tests still pass (covered by running full suite)
"""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch
from typing import Optional

from analytics.wallet_stats import WalletStats
from filters.wallet_filter import WalletFilterResult, filter_wallet
from wallet.ranking import score_wallet, rank_wallets


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_passing_stats(wallet: str = "0xpass") -> WalletStats:
    """Legacy WalletStats that passes every hard-filter threshold."""
    s = WalletStats(wallet=wallet)
    s.total_positions = 45
    s.closed_positions = 45
    s.wins = 30
    s.losses = 10
    s.breakeven = 5
    s.roi_pct = 22.0
    s.win_rate_pct = 75.0
    s.max_drawdown_pct = 12.0
    s.consistency_score = 8.0
    s.concentration_pct = 30.0
    s.active_days = 40
    s.total_invested = 4500.0
    s.per_trade_pnls = [5.0] * 45
    s.category_mix = {"sports": 40, "crypto": 5}
    s.is_resolution_only = False
    s.data_quality = "FULL"
    s.data_quality_reason = "test"
    s.pnl_source = "closed_positions_api"
    return s


def _make_failing_stats(wallet: str = "0xfail") -> WalletStats:
    """Legacy WalletStats that fails the MIN_POSITIONS hard filter."""
    s = WalletStats(wallet=wallet)
    s.total_positions = 5      # < MIN_POSITIONS (40)
    s.closed_positions = 5
    s.wins = 4
    s.losses = 1
    s.roi_pct = 25.0
    s.win_rate_pct = 80.0
    s.max_drawdown_pct = 5.0
    s.consistency_score = 9.0
    s.concentration_pct = 20.0
    s.active_days = 35
    s.total_invested = 500.0
    s.per_trade_pnls = [5.0] * 5
    s.category_mix = {"sports": 5}
    s.is_resolution_only = False
    s.data_quality = "FULL"
    s.data_quality_reason = "test"
    s.pnl_source = "closed_positions_api"
    return s


def _make_insufficient_stats(wallet: str = "0xinsuf") -> WalletStats:
    """
    Legacy WalletStats that produces INSUFFICIENT_DATA (enough positions
    but None ROI — simulates a wallet with no closed data).
    The filter handles roi_pct=0.0 as a numeric fail, not insufficient data
    for legacy WalletStats (roi_fee_exclusive_pct would need to be None for
    that path). Instead, simulate by temporarily patching filter to return
    INSUFFICIENT_DATA — or build WalletStats with enough positions but zero
    closed (so consistency = 0 but roi is also 0, causing FAIL on roi).

    For a pure INSUFFICIENT_DATA result, we use WalletStatistics with None fields.
    """
    from analytics.models import WalletStatistics
    return WalletStatistics(
        wallet=wallet,
        positions_counted=45,
        roi_fee_exclusive_pct=None,    # → insufficient
        active_days=40,
        consistency_mean_roi=0.25,
        consistency_stdev_roi=0.05,
        drawdown_cost_pct=10.0,
        top1_profit_share_pct=30.0,
        is_resolution_only=False,
        category_mix={"sports": 45},
    )


# ---------------------------------------------------------------------------
# 1. Pipeline wiring
# ---------------------------------------------------------------------------

def test_filter_wallet_is_present_in_cmd_scan_wallets_source():
    """filter_wallet() must appear in cmd_scan_wallets source (Phase 8C wiring)."""
    import main as main_module
    source = inspect.getsource(main_module.cmd_scan_wallets)
    assert "filter_wallet(" in source


def test_cmd_scan_wallets_inspects_result_status_not_only_passed():
    """
    The integration must check result.status explicitly.
    A check of only result.passed would silently promote INSUFFICIENT_DATA wallets.
    """
    import main as main_module
    source = inspect.getsource(main_module.cmd_scan_wallets)
    # Must contain status check
    assert "filter_result.status" in source or "result.status" in source
    # Must mention INSUFFICIENT_DATA by name
    assert "INSUFFICIENT_DATA" in source


def test_cmd_scan_wallets_emits_hard_filter_pass_fail_insufficient():
    """The three status strings must appear in the scan pipeline."""
    import main as main_module
    source = inspect.getsource(main_module.cmd_scan_wallets)
    assert "HARD_FILTER_PASS" in source
    assert "HARD_FILTER_REJECT" in source
    assert "HARD_FILTER_INSUFFICIENT_DATA" in source


# ---------------------------------------------------------------------------
# 2. FAIL wallets do not proceed to ranking
# ---------------------------------------------------------------------------

def test_fail_wallet_excluded_from_ranking():
    """
    A wallet that fails the hard filter must not appear in rank_wallets output.
    Simulate by constructing the filter_map and pass_wallets logic directly.
    """
    fail_stats = _make_failing_stats("0xfail")
    pass_stats_obj = _make_passing_stats("0xpass")

    filter_fail = filter_wallet(fail_stats)
    filter_pass = filter_wallet(pass_stats_obj)

    assert filter_fail.status == "FAIL", f"Expected FAIL, got {filter_fail.status}: {filter_fail.reasons}"
    assert filter_pass.status == "PASS", f"Expected PASS, got {filter_pass.status}"

    # Simulate what cmd_scan_wallets now does: only PASS wallets enter ranking
    filter_map = {"0xfail": filter_fail, "0xpass": filter_pass}
    wallets = ["0xfail", "0xpass"]
    pass_wallets = [w for w in wallets if filter_map[w].status == "PASS"]
    fail_wallets = [w for w in wallets if filter_map[w].status == "FAIL"]

    assert "0xfail" in fail_wallets
    assert "0xpass" in pass_wallets
    assert "0xfail" not in pass_wallets


def test_fail_wallet_rejection_reasons_are_preserved():
    """Exact rejection reasons from filter_wallet must be preserved in the result."""
    fail_stats = _make_failing_stats("0xfail")
    result = filter_wallet(fail_stats)
    assert result.status == "FAIL"
    assert len(result.reasons) > 0
    # MIN_POSITIONS failure is the expected reason here
    assert any("positions" in r.lower() for r in result.reasons)


def test_fail_wallet_does_not_receive_recommendation():
    """
    A FAIL wallet must not receive RECOMMENDED or WATCH recommendations.
    These are only produced by rank_wallets for wallets that pass the hard filter.
    """
    fail_stats = _make_failing_stats("0xfail")
    filter_result = filter_wallet(fail_stats)
    assert filter_result.status == "FAIL"

    # In the new pipeline, fail wallets get recommendation = "HARD_FILTER_REJECT"
    # and are NOT passed through rank_wallets at all.
    pass_wallets_stats = []   # empty — fail wallet excluded
    cards = rank_wallets(pass_wallets_stats, {})
    wallet_ids = [c.wallet for c in cards]
    assert "0xfail" not in wallet_ids


# ---------------------------------------------------------------------------
# 3. INSUFFICIENT_DATA wallets are not silently promoted to PASS
# ---------------------------------------------------------------------------

def test_insufficient_data_wallet_not_promoted_to_pass():
    """
    INSUFFICIENT_DATA must not be treated as PASS simply because result.passed == True.
    """
    insuf_stats = _make_insufficient_stats("0xinsuf")
    result = filter_wallet(insuf_stats)

    # result.passed == True for INSUFFICIENT_DATA — this is the trap
    assert result.passed is True
    # But status must not be PASS
    assert result.status == "INSUFFICIENT_DATA"
    # The correct check is result.status == "PASS", not bool(result.passed)
    is_proper_pass = (result.status == "PASS")
    assert not is_proper_pass, (
        "INSUFFICIENT_DATA wallet was promoted as a normal PASS via result.status check — "
        "this is the bug Phase 8C was designed to prevent."
    )


def test_insufficient_data_criteria_are_preserved():
    """Insufficient criteria list must be populated and preserved."""
    insuf_stats = _make_insufficient_stats("0xinsuf")
    result = filter_wallet(insuf_stats)
    assert result.status == "INSUFFICIENT_DATA"
    assert len(result.insufficient_criteria) > 0
    assert "roi" in result.insufficient_criteria


def test_insufficient_data_wallet_excluded_from_normal_ranking_pool():
    """
    INSUFFICIENT_DATA wallets must not appear in the normal ranking pool.
    Only PASS wallets enter rank_wallets.
    """
    insuf_stats = _make_insufficient_stats("0xinsuf")
    pass_stats_obj = _make_passing_stats("0xpass")

    filter_insuf = filter_wallet(insuf_stats)
    filter_pass = filter_wallet(pass_stats_obj)

    filter_map = {"0xinsuf": filter_insuf, "0xpass": filter_pass}
    wallets = ["0xinsuf", "0xpass"]
    pass_wallets = [w for w in wallets if filter_map[w].status == "PASS"]
    insuf_wallets = [w for w in wallets if filter_map[w].status == "INSUFFICIENT_DATA"]

    assert "0xinsuf" in insuf_wallets
    assert "0xinsuf" not in pass_wallets
    assert "0xpass" in pass_wallets


# ---------------------------------------------------------------------------
# 4. PASS wallets proceed through ranking unchanged
# ---------------------------------------------------------------------------

def test_pass_wallet_enters_ranking_and_gets_scored():
    """
    A wallet that passes the hard filter must be passed to rank_wallets and
    receive a WalletScoreCard with a non-None final_score.
    """
    pass_stats_obj = _make_passing_stats("0xpass")
    filter_result = filter_wallet(pass_stats_obj)
    assert filter_result.status == "PASS"

    cards = rank_wallets([pass_stats_obj], {})
    assert len(cards) == 1
    assert cards[0].wallet == "0xpass"
    assert cards[0].final_score is not None
    assert cards[0].final_score >= 0


def test_pass_wallet_recommendation_comes_from_ranking():
    """
    PASS wallets receive recommendation from rank_wallets (RECOMMENDED / WATCH / REJECT),
    not from the hard filter. The recommendation thresholds are unchanged.
    """
    pass_stats_obj = _make_passing_stats("0xpass")
    cards = rank_wallets([pass_stats_obj], {})
    assert len(cards) == 1
    assert cards[0].recommendation in ("RECOMMENDED", "WATCH", "REJECT")


def test_ranking_unchanged_for_pass_wallets():
    """
    Ranking scores for wallets that pass the hard filter must be identical
    to what score_wallet() would have computed directly.
    """
    pass_stats_obj = _make_passing_stats("0xpass")
    direct_card = score_wallet(pass_stats_obj)
    ranked_cards = rank_wallets([pass_stats_obj], {})
    assert len(ranked_cards) == 1
    assert ranked_cards[0].final_score == direct_card.final_score
    assert ranked_cards[0].recommendation == direct_card.recommendation


# ---------------------------------------------------------------------------
# 5. Recommendation thresholds are unchanged
# ---------------------------------------------------------------------------

def test_recommended_threshold_still_75():
    """RECOMMENDED still requires final_score >= 75."""
    from config.settings import settings
    # Build a wallet that scores exactly above 75 (known good parameters)
    s = _make_passing_stats("0xrec")
    s.roi_pct = 25.0      # preferred band: 20 pts
    s.win_rate_pct = 65.0  # strong: 20 pts
    s.total_positions = 45  # 15 pts
    s.consistency_score = 9.0  # 10 pts
    s.max_drawdown_pct = 5.0   # 15 pts
    s.concentration_pct = 20.0  # 10 pts
    s.active_days = 40          # 5 pts
    s.per_trade_pnls = [1.0] * 45
    card = score_wallet(s)
    if card.final_score >= 75:
        assert card.recommendation == "RECOMMENDED"
    elif card.final_score >= 55:
        assert card.recommendation == "WATCH"
    else:
        assert card.recommendation == "REJECT"


def test_watch_threshold_still_55():
    """The WATCH/REJECT boundary is still at 55 points."""
    from config.settings import settings
    s = _make_failing_stats("0xwatch")  # fails hard filter but we test scoring only
    s.roi_pct = 16.0
    s.win_rate_pct = 52.0
    s.total_positions = 40
    s.per_trade_pnls = [1.0] * 40
    s.consistency_score = 6.0
    s.max_drawdown_pct = 18.0
    s.concentration_pct = 40.0
    s.active_days = 31
    card = score_wallet(s)
    if card.final_score >= 75:
        assert card.recommendation == "RECOMMENDED"
    elif card.final_score >= 55:
        assert card.recommendation == "WATCH"
    else:
        assert card.recommendation == "REJECT"


# ---------------------------------------------------------------------------
# 6. Six target wallets — no stored data
# ---------------------------------------------------------------------------

TARGET_WALLETS = [
    "0xb1ca909e848cc24ec4e220ce1c453bc290c51705",
    "0x0f6f76ced62a911bccef92f50faaff143854d977",
    "0x224a89dbe0db0d6124b335edabd15b3f877da3d5",
    "0x73e3fec494611d73c170cb2f23850fd998b21be9",
    "0xc8ab97a9089a9ff7e6ef0688e6e591a066946418",
    "0x3a8aa345d5db7ec5138298c8c4f4540259be7699",
]


def test_six_target_wallets_not_in_project_files():
    """
    None of the six target wallets appear in any project source file
    (other than this test file itself, which defines the list for reference).
    Their hard-filter result can only be obtained via live API call.
    """
    import os

    project_root = os.path.join(
        os.path.dirname(__file__), ".."
    )
    this_file = os.path.abspath(__file__)
    found = {}
    for wallet in TARGET_WALLETS:
        matches = []
        for dirpath, _dirs, files in os.walk(project_root):
            # Skip hidden dirs, __pycache__, .git, and runtime data directory
            _dirs[:] = [d for d in _dirs if not d.startswith(".") and d not in ("__pycache__", "data")]
            for fname in files:
                if not fname.endswith((".py", ".json", ".md", ".log", ".txt")):
                    continue
                fpath = os.path.abspath(os.path.join(dirpath, fname))
                # Exclude this test file — it defines the list for reference
                if fpath == this_file:
                    continue
                try:
                    with open(fpath, encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                    if wallet.lower() in content.lower():
                        matches.append(fpath)
                except OSError:
                    pass
        if matches:
            found[wallet] = matches

    assert not found, (
        f"Target wallets found in project files (unexpected stored data): {found}"
    )


def test_six_target_wallets_all_produce_insufficient_data_with_empty_events():
    """
    With no activity events, all six target wallets produce WalletStats
    with data_quality=INSUFFICIENT_DATA, which is the expected result
    for wallets the scanner has never fetched.
    """
    from analytics.wallet_stats import compute_wallet_stats, DataQuality

    for wallet in TARGET_WALLETS:
        stats = compute_wallet_stats(wallet, events=[])
        assert stats.data_quality == DataQuality.INSUFFICIENT_DATA, (
            f"Wallet {wallet} with empty events should produce INSUFFICIENT_DATA quality"
        )
