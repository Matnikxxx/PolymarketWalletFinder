"""
Tests for the candidate-limit configuration path (Phase C follow-up).

Reported symptom: `MAX_WALLET_CANDIDATES=100` was set in `.env`, yet the scan
still reported 30 candidates. Three independent mechanisms can each produce
exactly that, and all three are covered here:

1. `.env` silently losing to a pre-existing process-environment variable
   (python-dotenv's `override=False` default) -> the value stays at its default.
2. A malformed/annotated value (`"100"`, `100 # comment`, `100.0`, `abc`)
   raising ValueError and silently falling back to the default.
3. A short leaderboard page being treated as end-of-data, truncating the walk
   (20 leaderboard rows + 10 seeds = 30) even when the limit was read correctly.

Configuration parsing, the effective-value source, and the guaranteed
respecting of the configured limit (including duplicate handling) are tested
here; the discovery/pagination mechanics live in test_scanner_pagination.py.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from config.settings import (
    CONFIG_ISSUES,
    parse_bool,
    parse_float,
    parse_int,
    setting_source,
)
from polymarket.api_client import PolymarketApiClient
from wallet.scanner import SEED_CANDIDATE_WALLETS, discover_candidates

SEEDS = [s.lower() for s in SEED_CANDIDATE_WALLETS]


def _addr(index: int) -> str:
    return "0x" + f"{index:040x}"


# ---------------------------------------------------------------------------
# 1. Value parsing robustness
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("100", 100),
    ("  100  ", 100),
    ("100\n", 100),
    ('"100"', 100),
    ("'100'", 100),
    ("100 # raise this later", 100),
    ("100.0", 100),          # whole-number float tolerated
    ("1_000", 1000),         # python int() accepts underscores
])
def test_parse_int_accepts_the_ways_people_actually_write_values(monkeypatch, raw, expected):
    monkeypatch.setenv("TEST_INT_SETTING", raw)
    assert parse_int("TEST_INT_SETTING", 30) == expected


@pytest.mark.parametrize("raw", ["abc", "1e2", "12.5", "", "ten"])
def test_parse_int_rejects_real_garbage_but_never_silently(monkeypatch, raw):
    monkeypatch.setenv("TEST_INT_SETTING", raw)
    CONFIG_ISSUES.clear()

    value = parse_int("TEST_INT_SETTING", 30)

    assert value == 30                                        # safe default kept
    assert any("TEST_INT_SETTING" in issue for issue in CONFIG_ISSUES), (
        "an unparseable value must be recorded, not silently swallowed"
    )


def test_parse_float_and_bool_records_issues_too(monkeypatch):
    monkeypatch.setenv("TEST_FLOAT_SETTING", "not-a-number")
    monkeypatch.setenv("TEST_BOOL_SETTING", "maybe")
    CONFIG_ISSUES.clear()

    assert parse_float("TEST_FLOAT_SETTING", 1.5) == 1.5
    assert parse_bool("TEST_BOOL_SETTING", True) is True
    assert sum(1 for issue in CONFIG_ISSUES if "TEST_FLOAT_SETTING" in issue) == 1
    assert sum(1 for issue in CONFIG_ISSUES if "TEST_BOOL_SETTING" in issue) == 1


def test_unset_setting_uses_the_default_without_an_issue(monkeypatch):
    monkeypatch.delenv("TEST_UNSET_SETTING", raising=False)
    CONFIG_ISSUES.clear()

    assert parse_int("TEST_UNSET_SETTING", 42) == 42
    assert not CONFIG_ISSUES


# ---------------------------------------------------------------------------
# 2. Effective-value source (the "I changed .env and nothing happened" case)
# ---------------------------------------------------------------------------

def test_setting_source_reports_default_when_unset(monkeypatch):
    monkeypatch.delenv("TEST_SOURCE_SETTING", raising=False)
    assert setting_source("TEST_SOURCE_SETTING") == "default"


def test_setting_source_distinguishes_env_file_from_process_environment(monkeypatch):
    import config.settings as cfg

    monkeypatch.setattr(cfg, "_ENV_FILE_VALUES", {"TEST_SOURCE_SETTING": "100"})

    monkeypatch.setenv("TEST_SOURCE_SETTING", "100")
    assert setting_source("TEST_SOURCE_SETTING") == ".env"

    # A process-environment value that disagrees with .env wins -- and is
    # reported as such, which is what makes the symptom diagnosable.
    monkeypatch.setenv("TEST_SOURCE_SETTING", "30")
    assert setting_source("TEST_SOURCE_SETTING") == "process environment"


def test_env_file_shadowing_is_recorded_as_a_config_issue(monkeypatch):
    import config.settings as cfg

    monkeypatch.setattr(cfg, "_ENV_FILE_VALUES", {"TEST_SHADOW_SETTING": "100"})
    monkeypatch.setenv("TEST_SHADOW_SETTING", "30")
    CONFIG_ISSUES.clear()

    assert cfg.parse_int("TEST_SHADOW_SETTING", 30) == 30

    assert any(
        "TEST_SHADOW_SETTING" in issue and ".env says" in issue and "process environment" in issue
        for issue in CONFIG_ISSUES
    ), "shadowing a .env value must be reported, not hidden"


# ---------------------------------------------------------------------------
# 3. The configured limit is actually respected
# ---------------------------------------------------------------------------

def _clipping_client(rows_per_page: int) -> MagicMock:
    """Client whose leaderboard clips every page to `rows_per_page` rows."""
    client = MagicMock(spec=PolymarketApiClient)

    def _get_leaderboard(**kwargs):
        start = int(kwargs.get("offset", 0))
        rows = min(rows_per_page, int(kwargs.get("limit", rows_per_page)))
        return [{"proxyWallet": _addr(start + i + 1)} for i in range(rows)]

    client.get_leaderboard.side_effect = _get_leaderboard
    return client


def test_configured_limit_of_100_is_reached_even_when_pages_are_clipped_to_20():
    """
    The exact reported scenario: limit 100, server returns 20 rows per page.
    Before the fix this stopped at 20 leaderboard + 10 seeds = 30.
    """
    client = _clipping_client(20)

    result = discover_candidates(client, limit=100, page_size=50, max_pages=10)

    assert result.total_candidates == 100
    assert result.leaderboard_candidates == 90
    assert result.seed_candidates == 10
    assert result.stop_reason == "leaderboard_budget"
    assert len(set(result.candidates)) == 100          # nothing counted twice


def test_configured_limit_is_reached_for_several_limits():
    for limit in (30, 50, 100, 137):
        client = _clipping_client(20)
        result = discover_candidates(client, limit=limit, page_size=50, max_pages=10)
        assert result.total_candidates == limit, limit
        assert len(set(result.candidates)) == limit, limit


def test_candidate_count_never_exceeds_the_configured_limit():
    client = _clipping_client(50)

    for limit in (5, 10, 11, 29, 30, 60, 99):
        result = discover_candidates(client, limit=limit, page_size=50, max_pages=10)
        assert result.total_candidates <= limit, limit


def test_duplicate_wallets_are_not_counted_twice_in_the_supplied_list():
    """Overlapping pages must never inflate the reported candidate count."""
    pages = {
        0: [{"proxyWallet": _addr(i)} for i in range(1, 21)],
        20: [{"proxyWallet": _addr(i)} for i in range(11, 31)],   # 10 overlapping rows
    }
    client = MagicMock(spec=PolymarketApiClient)
    client.get_leaderboard.side_effect = lambda **kw: pages.get(int(kw.get("offset", 0)), [])

    result = discover_candidates(client, limit=40, page_size=20, max_pages=5)

    assert result.duplicates_dropped == 10
    assert result.total_candidates == len(set(result.candidates))
    assert result.leaderboard_candidates == 30


def test_configured_limit_below_the_seed_count_is_still_respected():
    client = _clipping_client(20)

    result = discover_candidates(client, limit=4, page_size=50, max_pages=10)

    assert result.total_candidates == 4
    assert result.seed_candidates == 0          # no room once the board filled it


def test_reachability_is_reported_when_the_page_cap_is_the_binding_knob():
    client = _clipping_client(50)

    result = discover_candidates(client, limit=100, page_size=50, max_pages=1)

    assert result.total_candidates == 60        # 1 page x 50 rows + 10 seeds
    assert result.stop_reason == "page_cap"
    assert result.notes and "not reachable" in result.notes[0]
    assert "MAX_LEADERBOARD_PAGES" in result.notes[0]


def test_no_reachability_note_when_the_limit_is_reachable():
    client = _clipping_client(50)

    result = discover_candidates(client, limit=100, page_size=50, max_pages=5)

    assert result.total_candidates == 100
    assert not result.notes


# ---------------------------------------------------------------------------
# 4. Diagnostics surface the effective configuration
# ---------------------------------------------------------------------------

def test_scan_report_prints_effective_limit_with_its_source(capsys, monkeypatch):
    from monitor.scan_report import build_scan_summary, format_scan_summary

    summary = build_scan_summary(wallets=[], existing_records=[])
    text = format_scan_summary(summary)

    assert "Configuration in force" in text
    assert f"MAX_WALLET_CANDIDATES : {settings_limit()}" in text
    assert "from " in text                       # source is always stated


def settings_limit() -> int:
    from config.settings import settings
    return settings.MAX_WALLET_CANDIDATES


def test_scan_report_warns_when_the_process_environment_supplied_the_limit(monkeypatch):
    from monitor.scan_report import build_scan_summary, format_scan_summary

    monkeypatch.setenv("MAX_WALLET_CANDIDATES", str(settings_limit() + 1))
    text = format_scan_summary(build_scan_summary(wallets=[]))

    assert "process environment" in text and "supplied MAX_WALLET_CANDIDATES" in text


def test_scan_report_surfaces_config_issues(monkeypatch):
    from monitor.scan_report import build_scan_summary, format_scan_summary

    CONFIG_ISSUES.clear()
    CONFIG_ISSUES.append("MAX_WALLET_CANDIDATES='abc' is not a valid integer; falling back to 30.")
    text = format_scan_summary(build_scan_summary(wallets=[]))

    assert "CONFIG ISSUE: MAX_WALLET_CANDIDATES='abc'" in text
    CONFIG_ISSUES.clear()


def test_scan_report_shows_per_page_rows_and_short_page_count():
    from monitor.scan_report import build_scan_summary, format_scan_summary

    class _Discovery:
        pages_attempted = 5
        pages_succeeded = 5
        rows_returned = 90
        rejected_addresses = 0
        duplicates_dropped = 0
        leaderboard_candidates = 90
        seed_candidates = 10
        seed_duplicates = 0
        leaderboard_budget = 90
        stop_reason = "leaderboard_budget"
        page_row_counts = [20, 20, 20, 20, 10]
        short_pages = 4
        notes = []
        sources = {}

    text = format_scan_summary(build_scan_summary(wallets=[], discovery=_Discovery()))

    assert "rows per page          : [20, 20, 20, 20, 10]" in text
    assert "short pages: 4" in text
    assert "walked through, not treated as the end" in text
