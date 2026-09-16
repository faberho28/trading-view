from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from trading_bot.domain.models import Action, Position, Signal
from trading_bot.risk.guardrails import (
    CorrelationCapGuardrail,
    DailyLossCapGuardrail,
    KillSwitchGuardrail,
    MarketHoursWindowGuardrail,
    MaxOpenPositionsGuardrail,
    MaxRiskPerTradeGuardrail,
    MinRewardRiskGuardrail,
    PositionSizingCapGuardrail,
    SlippageGuardGuardrail,
)
from tests.conftest import make_ctx


def test_max_risk_per_trade_pass(buy_signal, passing_ctx):
    g = MaxRiskPerTradeGuardrail(max_risk_pct=0.01)
    # stop distance = 2, qty=1 -> risked 2 << 1% of 100000 = 1000
    result = g.check(buy_signal, passing_ctx, proposed_qty=1)
    assert result.passed


def test_max_risk_per_trade_fail(buy_signal, passing_ctx):
    g = MaxRiskPerTradeGuardrail(max_risk_pct=0.01)
    # stop distance = 2, qty=1000 -> risked 2000 > 1000
    result = g.check(buy_signal, passing_ctx, proposed_qty=1000)
    assert not result.passed


def test_position_sizing_cap_pass(buy_signal, passing_ctx):
    g = PositionSizingCapGuardrail(max_risk_pct=0.01, max_position_value_pct=0.10)
    result = g.check(buy_signal, passing_ctx, proposed_qty=10)
    assert result.passed


def test_position_sizing_cap_fail(buy_signal, passing_ctx):
    g = PositionSizingCapGuardrail(max_risk_pct=0.01, max_position_value_pct=0.10)
    # 200 qty * 100 price = 20000 > 10% of 100000 = 10000
    result = g.check(buy_signal, passing_ctx, proposed_qty=200)
    assert not result.passed


def test_compute_qty_clamps_to_position_cap_when_stop_is_tight(
    sample_account, no_positions, market_open_clock, passing_session_snapshot
):
    """Regression for the 2026-09-15 production incident: a stop distance
    equal to the risk percentage (both 1%) made risk-only sizing propose
    ~100% of equity, guaranteeing position_sizing_cap rejected every
    signal. compute_qty must clamp to the cap instead of ignoring it."""
    signal = Signal(
        symbol="GOOGL",
        action=Action.BUY,
        confidence=0.6,
        reference_price=Decimal("333.55"),
        stop_price=Decimal("330.2145"),  # 1% below reference, like the real incident
        target_price=Decimal("340.22"),
        rationale="test tight stop",
        generated_at=datetime.now(timezone.utc),
    )
    ctx = make_ctx(sample_account, no_positions, passing_session_snapshot, Decimal("333.55"), market_open_clock)
    g = PositionSizingCapGuardrail(max_risk_pct=0.01, max_position_value_pct=0.10)

    qty = g.compute_qty(signal, ctx)

    position_value = qty * signal.reference_price
    max_allowed = ctx.account.equity * Decimal("0.10")
    assert position_value <= max_allowed
    assert qty > 0  # still proposes a real, smaller trade instead of nothing


def test_daily_loss_cap_pass(buy_signal, passing_ctx):
    g = DailyLossCapGuardrail(daily_loss_cap_pct=0.03)
    result = g.check(buy_signal, passing_ctx, proposed_qty=1)
    assert result.passed


def test_daily_loss_cap_fail(buy_signal, sample_account, no_positions, market_open_clock):
    from trading_bot.domain.models import SessionStateSnapshot

    breached = SessionStateSnapshot(
        daily_pnl_pct=-0.05, consecutive_losses=0, api_errors_in_window=0, halted=False
    )
    ctx = make_ctx(sample_account, no_positions, breached, Decimal("100"), market_open_clock)
    g = DailyLossCapGuardrail(daily_loss_cap_pct=0.03)
    result = g.check(buy_signal, ctx, proposed_qty=1)
    assert not result.passed


def test_max_open_positions_pass(buy_signal, passing_ctx):
    g = MaxOpenPositionsGuardrail(max_open_positions=5)
    result = g.check(buy_signal, passing_ctx, proposed_qty=1)
    assert result.passed


def test_max_open_positions_fail(
    buy_signal, sample_account, passing_session_snapshot, market_open_clock
):
    positions = [
        Position(symbol=s, qty=1, avg_entry_price=Decimal("1"), unrealized_pl=Decimal("0"), sector="tech")
        for s in ["MSFT", "SPY", "NVDA", "GOOGL", "TSLA"]
    ]
    ctx = make_ctx(sample_account, positions, passing_session_snapshot, Decimal("100"), market_open_clock)
    g = MaxOpenPositionsGuardrail(max_open_positions=5)
    result = g.check(buy_signal, ctx, proposed_qty=1)
    assert not result.passed


def test_min_reward_risk_pass(buy_signal, passing_ctx):
    g = MinRewardRiskGuardrail(min_rr=2.0)
    result = g.check(buy_signal, passing_ctx, proposed_qty=1)
    assert result.passed


def test_min_reward_risk_fail(buy_signal, passing_ctx):
    bad_signal = replace(buy_signal, target_price=Decimal("101"))  # rr = 1/2 = 0.5
    g = MinRewardRiskGuardrail(min_rr=2.0)
    result = g.check(bad_signal, passing_ctx, proposed_qty=1)
    assert not result.passed


def test_correlation_cap_pass(buy_signal, passing_ctx):
    g = CorrelationCapGuardrail(basket_max=2)
    result = g.check(buy_signal, passing_ctx, proposed_qty=1)
    assert result.passed


def test_correlation_cap_fail(buy_signal, sample_account, passing_session_snapshot, market_open_clock):
    positions = [
        Position(symbol="MSFT", qty=1, avg_entry_price=Decimal("1"), unrealized_pl=Decimal("0"), sector="tech"),
        Position(symbol="NVDA", qty=1, avg_entry_price=Decimal("1"), unrealized_pl=Decimal("0"), sector="tech"),
    ]
    ctx = make_ctx(sample_account, positions, passing_session_snapshot, Decimal("100"), market_open_clock)
    g = CorrelationCapGuardrail(basket_max=2)
    result = g.check(buy_signal, ctx, proposed_qty=1)  # AAPL would be a 3rd tech position
    assert not result.passed


def test_no_duplicate_position_blocks_reentry_on_already_open_symbol(
    buy_signal, sample_account, passing_session_snapshot, market_open_clock
):
    """Regression for the 2026-09-16 production incident: with no memory
    across cycles, the orchestrator bought NVDA three separate times and
    shorted MSFT twice, each treated as a brand-new entry. A symbol
    already holding a position must be skipped entirely."""
    from trading_bot.risk.guardrails import NoDuplicatePositionGuardrail

    positions = [
        Position(symbol="AAPL", qty=29, avg_entry_price=Decimal("334.12"), unrealized_pl=Decimal("0"), sector="unknown"),
    ]
    ctx = make_ctx(sample_account, positions, passing_session_snapshot, Decimal("100"), market_open_clock)
    g = NoDuplicatePositionGuardrail()

    result = g.check(buy_signal, ctx, proposed_qty=1)  # buy_signal.symbol == "AAPL"

    assert not result.passed


def test_no_duplicate_position_allows_symbol_with_no_open_position(buy_signal, passing_ctx):
    from trading_bot.risk.guardrails import NoDuplicatePositionGuardrail

    g = NoDuplicatePositionGuardrail()
    result = g.check(buy_signal, passing_ctx, proposed_qty=1)
    assert result.passed


def test_correlation_cap_uses_static_basket_not_broker_reported_sector(
    buy_signal, sample_account, passing_session_snapshot, market_open_clock
):
    """Regression for the 2026-09-16 production incident: Alpaca's Position
    API has no sector field, so AlpacaBrokerAdapter.get_positions() always
    reports sector="unknown". Trusting that collapsed every open position
    into one fake basket, letting a 3rd/4th tech position through (or, as
    seen live, blocking a genuinely diversified position) once anything
    was already held. The guardrail must derive sector from the static
    watchlist basket, never from Position.sector."""
    positions = [
        Position(symbol="MSFT", qty=1, avg_entry_price=Decimal("1"), unrealized_pl=Decimal("0"), sector="unknown"),
        Position(symbol="NVDA", qty=1, avg_entry_price=Decimal("1"), unrealized_pl=Decimal("0"), sector="unknown"),
    ]
    ctx = make_ctx(sample_account, positions, passing_session_snapshot, Decimal("100"), market_open_clock)
    g = CorrelationCapGuardrail(basket_max=2)
    result = g.check(buy_signal, ctx, proposed_qty=1)  # AAPL would be a 3rd tech position
    assert not result.passed
    assert result.computed["sector"] == "tech"


def test_market_hours_window_pass(buy_signal, passing_ctx):
    g = MarketHoursWindowGuardrail(edge_buffer_minutes=5)
    result = g.check(buy_signal, passing_ctx, proposed_qty=1)
    assert result.passed


def test_market_hours_window_fail_near_close(buy_signal, sample_account, no_positions, passing_session_snapshot):
    from trading_bot.domain.models import MarketClock

    # next_open must stay in the future even while open (real Alpaca
    # semantics) — this case exercises the close-side edge buffer instead.
    now = datetime(2026, 9, 8, 19, 58, 0, tzinfo=timezone.utc)
    clock = MarketClock(
        is_open=True, next_open=now + timedelta(hours=14), next_close=now + timedelta(minutes=2), now=now
    )
    ctx = make_ctx(sample_account, no_positions, passing_session_snapshot, Decimal("100"), clock)
    g = MarketHoursWindowGuardrail(edge_buffer_minutes=5)
    result = g.check(buy_signal, ctx, proposed_qty=1)
    assert not result.passed


def test_market_hours_window_closed(buy_signal, sample_account, no_positions, passing_session_snapshot):
    from trading_bot.domain.models import MarketClock

    now = datetime(2026, 9, 8, 3, 0, 0, tzinfo=timezone.utc)
    clock = MarketClock(
        is_open=False, next_open=now + timedelta(hours=10), next_close=now + timedelta(hours=16), now=now
    )
    ctx = make_ctx(sample_account, no_positions, passing_session_snapshot, Decimal("100"), clock)
    g = MarketHoursWindowGuardrail(edge_buffer_minutes=5)
    result = g.check(buy_signal, ctx, proposed_qty=1)
    assert not result.passed


def test_slippage_guard_pass(buy_signal, passing_ctx):
    g = SlippageGuardGuardrail(max_slippage_pct=0.003)
    result = g.check(buy_signal, passing_ctx, proposed_qty=1)
    assert result.passed


def test_slippage_guard_fail(buy_signal, sample_account, no_positions, passing_session_snapshot, market_open_clock):
    ctx = make_ctx(sample_account, no_positions, passing_session_snapshot, Decimal("105"), market_open_clock)
    g = SlippageGuardGuardrail(max_slippage_pct=0.003)
    result = g.check(buy_signal, ctx, proposed_qty=1)
    assert not result.passed


def test_kill_switch_pass(buy_signal, passing_ctx):
    g = KillSwitchGuardrail()
    result = g.check(buy_signal, passing_ctx, proposed_qty=1)
    assert result.passed


def test_kill_switch_fail(buy_signal, sample_account, no_positions, halted_session_snapshot, market_open_clock):
    ctx = make_ctx(sample_account, no_positions, halted_session_snapshot, Decimal("100"), market_open_clock)
    g = KillSwitchGuardrail()
    result = g.check(buy_signal, ctx, proposed_qty=1)
    assert not result.passed


def test_risk_manager_aggregates_multiple_failures(buy_signal, passing_ctx):
    from trading_bot.config import RiskThresholds
    from trading_bot.risk.manager import RiskManager

    thresholds = RiskThresholds(
        max_risk_per_trade_pct=0.0001,  # will fail max_risk_per_trade with normal qty
        max_position_value_pct=0.10,
        daily_loss_cap_pct=0.03,
        max_open_positions=5,
        min_reward_risk_ratio=100.0,  # forces min_reward_risk_ratio failure
        correlation_basket_max=2,
        slippage_max_pct=0.003,
        market_edge_buffer_minutes=5,
        kill_switch_consecutive_losses=3,
        kill_switch_api_errors=5,
        kill_switch_api_error_window_minutes=15,
    )
    manager = RiskManager(thresholds)
    decision = manager.evaluate(buy_signal, passing_ctx)
    assert not decision.approved
    assert "min_reward_risk_ratio" in decision.rejection_reasons


def test_risk_manager_kill_switch_short_circuits(
    buy_signal, sample_account, no_positions, halted_session_snapshot, market_open_clock
):
    from trading_bot.config import RiskThresholds
    from trading_bot.risk.manager import RiskManager

    thresholds = RiskThresholds(
        max_risk_per_trade_pct=0.01,
        max_position_value_pct=0.10,
        daily_loss_cap_pct=0.03,
        max_open_positions=5,
        min_reward_risk_ratio=2.0,
        correlation_basket_max=2,
        slippage_max_pct=0.003,
        market_edge_buffer_minutes=5,
        kill_switch_consecutive_losses=3,
        kill_switch_api_errors=5,
        kill_switch_api_error_window_minutes=15,
    )
    manager = RiskManager(thresholds)
    ctx = make_ctx(sample_account, no_positions, halted_session_snapshot, Decimal("100"), market_open_clock)
    decision = manager.evaluate(buy_signal, ctx)
    assert not decision.approved
    assert decision.rejection_reasons == ["kill_switch_active"]
    assert len(decision.checks) == 1
