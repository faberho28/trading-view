from __future__ import annotations

from trading_bot.domain.models import Action
from trading_bot.signals.technical import TechnicalSignal
from tests.conftest import make_bars


def test_breakout_buy_signal_with_all_filters_passing():
    # Two touches near 105 (105, then 104.9 — within 1% tolerance) confirm a
    # strong resistance. Trend MA(5) over the last 5 closes sits below the
    # breakout close (uptrend), and the breakout bar's volume (2000) clears
    # 1.2x the trailing baseline (1000).
    closes = [100, 102, 105, 102, 100, 101, 104.9, 102, 100, 103, 108]
    volumes = [1000] * 10 + [2000]
    bars = make_bars("AAPL", closes, volumes)
    signal = TechnicalSignal(
        pivot_window=2, trend_ma_period=5, volume_lookback=5,
        volume_confirmation_multiplier=1.2, min_level_touches=2,
        level_touch_tolerance_pct=0.01,
    ).generate("AAPL", bars)
    assert signal.action == Action.BUY
    assert 0.0 <= signal.confidence <= 1.0


def test_breakout_sell_signal_with_all_filters_passing():
    closes = [100, 98, 95, 98, 100, 99, 95.1, 98, 100, 97, 90]
    volumes = [1000] * 10 + [2000]
    bars = make_bars("AAPL", closes, volumes)
    signal = TechnicalSignal(
        pivot_window=2, trend_ma_period=5, volume_lookback=5,
        volume_confirmation_multiplier=1.2, min_level_touches=2,
        level_touch_tolerance_pct=0.01,
    ).generate("AAPL", bars)
    assert signal.action == Action.SELL
    assert 0.0 <= signal.confidence <= 1.0


def test_single_touch_level_is_not_strong_enough():
    # Same shape as the passing BUY case but with only ONE touch of the
    # resistance (no second pivot near 105) — min_level_touches=2 must
    # reject it as not-yet-confirmed support/resistance, not real S/R.
    closes = [100, 102, 105, 102, 100, 101, 100, 99, 100, 103, 108]
    volumes = [1000] * 10 + [2000]
    bars = make_bars("AAPL", closes, volumes)
    signal = TechnicalSignal(
        pivot_window=2, trend_ma_period=5, volume_lookback=5,
        volume_confirmation_multiplier=1.2, min_level_touches=2,
        level_touch_tolerance_pct=0.01,
    ).generate("AAPL", bars)
    assert signal.action == Action.HOLD


def test_counter_trend_breakout_is_blocked():
    # Price breaks above the confirmed resistance (105) but the last few
    # bars ran up so high (130) that the trend MA(3) sits above the
    # breakout close — a counter-trend breakout, must be rejected even
    # though the raw pivot break is real.
    closes = [100, 102, 105, 102, 100, 130, 108]
    bars = make_bars("AAPL", closes)
    signal = TechnicalSignal(
        pivot_window=2, trend_ma_period=3, volume_lookback=2,
        volume_confirmation_multiplier=0.0,  # isolate the trend filter
        min_level_touches=1, level_touch_tolerance_pct=0.01,
    ).generate("AAPL", bars)
    assert signal.action == Action.HOLD
    assert "trend" in signal.rationale


def test_low_volume_breakout_is_blocked():
    # Real resistance, real uptrend, but the breakout bar's volume (500) is
    # far below the trailing baseline (2000) — a breakout without volume
    # rarely holds and must be rejected.
    closes = [100, 102, 105, 102, 100, 101, 108]
    volumes = [1000, 1000, 1000, 2000, 2000, 2000, 500]
    bars = make_bars("AAPL", closes, volumes)
    signal = TechnicalSignal(
        pivot_window=2, trend_ma_period=2, volume_lookback=3,
        volume_confirmation_multiplier=1.2, min_level_touches=1,
        level_touch_tolerance_pct=0.01,
    ).generate("AAPL", bars)
    assert signal.action == Action.HOLD
    assert "volume" in signal.rationale


def test_hold_when_within_band():
    closes = [100, 101, 100, 99, 100, 101, 100, 99, 100, 100]
    bars = make_bars("AAPL", closes)
    signal = TechnicalSignal(
        pivot_window=2, trend_ma_period=5, volume_lookback=3, min_level_touches=1,
    ).generate("AAPL", bars)
    assert signal.action == Action.HOLD


def test_hold_when_insufficient_bars():
    bars = make_bars("AAPL", [100, 101, 102])
    signal = TechnicalSignal().generate("AAPL", bars)
    assert signal.action == Action.HOLD
    assert signal.confidence == 0.0
