"""
Tests for Phase 6 configuration additions and validation.
"""
from config.settings import settings


def test_breakeven_tolerance_setting_exists_and_defaults():
    assert hasattr(settings, "BREAKEVEN_TOLERANCE_USDC")
    assert settings.BREAKEVEN_TOLERANCE_USDC == 0.01
    assert isinstance(settings.BREAKEVEN_TOLERANCE_USDC, float)


def test_min_positions_setting_remains_40():
    assert settings.MIN_POSITIONS == 40
