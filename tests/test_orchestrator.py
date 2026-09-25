from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from trading_bot.audit import AuditLogger
from trading_bot.config import RiskThresholds, Settings
from trading_bot.domain.models import (
    Account,
    Action,
    MarketClock,
    OrderResult,
    Position,
    Signal,
)
from trading_bot.orchestrator import Orchestrator
from trading_bot.ports.broker import BrokerError, BrokerPort
from trading_bot.risk.manager import RiskManager
from tests.conftest import make_bars

WATCHLIST = ["AAPL", "MSFT", "SPY", "NVDA", "GOOGL"]


def _thresholds() -> RiskThresholds:
    return RiskThresholds(
        max_risk_per_trade_pct=0.01,  # loose but consistent, so guardrails don't block the smoke test
        max_position_value_pct=0.5,
        daily_loss_cap_pct=0.03,
        max_open_positions=5,
        min_reward_risk_ratio=0.1,
        correlation_basket_max=5,
        slippage_max_pct=0.5,
        market_edge_buffer_minutes=5,
        kill_switch_consecutive_losses=3,
        kill_switch_api_errors=5,
        kill_switch_api_error_window_minutes=15,
    )


class HoldSignalSource:
    def generate(self, symbol, bars):
        return Signal(
            symbol=symbol,
            action=Action.HOLD,
            confidence=0.0,
            reference_price=Decimal("100"),
            stop_price=None,
            target_price=None,
            rationale="hold",
            generated_at=datetime.now(timezone.utc),
        )


class BuySignalSource:
    def generate(self, symbol, bars):
        return Signal(
            symbol=symbol,
            action=Action.BUY,
            confidence=0.9,
            reference_price=Decimal("100"),
            stop_price=Decimal("98"),
            target_price=Decimal("104"),
            rationale="forced buy",
            generated_at=datetime.now(timezone.utc),
        )


class FailOneSymbolSignalSource:
    def __init__(self, fail_symbol: str):
        self.fail_symbol = fail_symbol

    def generate(self, symbol, bars):
        if symbol == self.fail_symbol:
            raise BrokerError("simulated failure")
        return Signal(
            symbol=symbol,
            action=Action.HOLD,
            confidence=0.0,
            reference_price=Decimal("100"),
            stop_price=None,
            target_price=None,
            rationale="hold",
            generated_at=datetime.now(timezone.utc),
        )


def _open_clock() -> MarketClock:
    # Mirrors real Alpaca semantics: next_open is always the *next*
    # scheduled open (tomorrow's, while today's session is in progress),
    # never a past timestamp. A fixture using a past next_open masked the
    # production bug where the orchestrator compared now - next_open.
    now = datetime(2026, 9, 8, 15, 0, tzinfo=timezone.utc)
    return MarketClock(
        is_open=True,
        next_open=now + timedelta(hours=19),
        next_close=now + timedelta(hours=5),
        now=now,
    )


def _mock_broker(mocker, clock=None):
    broker = mocker.Mock(spec=BrokerPort)
    broker.is_market_open.return_value = clock or _open_clock()
    broker.get_account.return_value = Account(
        equity=Decimal("100000"), cash=Decimal("50000"), buying_power=Decimal("100000")
    )
    broker.get_positions.return_value = []
    broker.get_bars.return_value = make_bars("AAPL", [100] * 10)
    broker.get_latest_price.return_value = Decimal("100")
    broker.place_order.return_value = OrderResult(
        order_id="order-1",
        client_order_id="AAPL-123",
        status="accepted",
        filled_qty=0,
        filled_avg_price=None,
        submitted_at=datetime.now(timezone.utc),
    )
    return broker


def test_end_to_end_cycle_hold_writes_one_record_per_symbol(mocker, tmp_path):
    broker = _mock_broker(mocker)
    audit_path = tmp_path / "audit.jsonl"
    orchestrator = Orchestrator(
        broker=broker,
        signal_source=HoldSignalSource(),
        risk_manager=RiskManager(_thresholds()),
        audit_logger=AuditLogger(audit_path),
        watchlist=WATCHLIST,
        lookback_bars=200,
    )
    orchestrator.run_cycle()

    lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == len(WATCHLIST)
    broker.place_order.assert_not_called()


def test_approved_trade_executes(mocker, tmp_path):
    broker = _mock_broker(mocker)
    audit_path = tmp_path / "audit.jsonl"
    orchestrator = Orchestrator(
        broker=broker,
        signal_source=BuySignalSource(),
        risk_manager=RiskManager(_thresholds()),
        audit_logger=AuditLogger(audit_path),
        watchlist=WATCHLIST,
        lookback_bars=200,
    )
    orchestrator.run_cycle()
    assert broker.place_order.call_count == len(WATCHLIST)


def test_rejected_trade_does_not_execute(mocker, tmp_path):
    broker = _mock_broker(mocker)
    audit_path = tmp_path / "audit.jsonl"
    strict_thresholds = RiskThresholds(
        max_risk_per_trade_pct=0.01,
        max_position_value_pct=0.9,
        daily_loss_cap_pct=0.03,
        max_open_positions=0,  # forces max_open_positions guardrail to always fail
        min_reward_risk_ratio=0.1,
        correlation_basket_max=5,
        slippage_max_pct=0.5,
        market_edge_buffer_minutes=5,
        kill_switch_consecutive_losses=3,
        kill_switch_api_errors=5,
        kill_switch_api_error_window_minutes=15,
    )
    orchestrator = Orchestrator(
        broker=broker,
        signal_source=BuySignalSource(),
        risk_manager=RiskManager(strict_thresholds),
        audit_logger=AuditLogger(audit_path),
        watchlist=WATCHLIST,
        lookback_bars=200,
    )
    orchestrator.run_cycle()
    broker.place_order.assert_not_called()

    lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == len(WATCHLIST)
    assert all('"final_action": "REJECTED"' in line for line in lines)


def test_halted_cycle_is_a_noop(mocker, tmp_path):
    broker = _mock_broker(mocker)
    audit_path = tmp_path / "audit.jsonl"
    orchestrator = Orchestrator(
        broker=broker,
        signal_source=BuySignalSource(),
        risk_manager=RiskManager(_thresholds()),
        audit_logger=AuditLogger(audit_path),
        watchlist=WATCHLIST,
        lookback_bars=200,
    )
    orchestrator.session_state.manual_reset()
    orchestrator.session_state._trigger_halt("test_halt")  # noqa: SLF001 - test-only direct trigger

    orchestrator.run_cycle()

    broker.is_market_open.assert_not_called()
    broker.place_order.assert_not_called()
    lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == len(WATCHLIST)


def test_broker_error_on_one_symbol_does_not_abort_the_rest(mocker, tmp_path):
    broker = _mock_broker(mocker)
    audit_path = tmp_path / "audit.jsonl"
    orchestrator = Orchestrator(
        broker=broker,
        signal_source=FailOneSymbolSignalSource(fail_symbol="MSFT"),
        risk_manager=RiskManager(_thresholds()),
        audit_logger=AuditLogger(audit_path),
        watchlist=WATCHLIST,
        lookback_bars=200,
    )
    orchestrator.run_cycle()

    lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == len(WATCHLIST)
    assert orchestrator.session_state.api_errors_in_window(15) == 1


def test_correlation_cap_sees_same_cycle_fills(mocker, tmp_path):
    """Regression for the production incident spanning 2026-09-16/17:
    positions is fetched once per cycle and shared by reference across
    every symbol. Without updating it after each EXECUTED trade,
    AAPL/MSFT/NVDA breaking out together (or across adjacent cycles before
    the first one's fill was visible) piled up as 3 simultaneous tech
    positions despite correlation_basket_max=2 — each guardrail check
    only saw the state from before the cycle started."""
    broker = _mock_broker(mocker)
    audit_path = tmp_path / "audit.jsonl"
    thresholds = _thresholds().model_copy(update={"correlation_basket_max": 2})
    orchestrator = Orchestrator(
        broker=broker,
        signal_source=BuySignalSource(),
        risk_manager=RiskManager(thresholds),
        audit_logger=AuditLogger(audit_path),
        watchlist=["AAPL", "MSFT", "NVDA"],  # all TECH_BASKET
        lookback_bars=200,
    )
    orchestrator.run_cycle()

    # Only 2 of the 3 tech-basket symbols should have executed; the 3rd
    # must be blocked by correlation_cap within the SAME cycle, not just
    # on a later one.
    assert broker.place_order.call_count == 2

    lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
    rejected = [line for line in lines if '"final_action": "REJECTED"' in line]
    assert len(rejected) == 1
    assert "correlation_cap" in rejected[0]


def test_reattaches_stop_when_position_has_none(mocker, tmp_path):
    """Regression for the 2026-09-25 production incident: GOOGL, MSFT, and
    SPY sat open for about a week with no active stop-loss order (the
    original bracket leg had been cancelled) and nothing noticed, losing
    well past the intended 1% risk. Every cycle must now detect this and
    re-attach a fallback stop immediately."""
    broker = _mock_broker(mocker)
    broker.get_positions.return_value = [
        Position(symbol="MSFT", qty=-20, avg_entry_price=Decimal("493.50"), unrealized_pl=Decimal("-478"), sector="tech"),
    ]
    broker.has_active_stop.return_value = False
    # Price already ran well past the entry-based 1% stop (the exact
    # failure this guards against) — the fallback must anchor to THIS,
    # not to avg_entry_price, or Alpaca rejects the order outright.
    broker.get_latest_price.return_value = Decimal("517.42")
    broker.submit_protective_stop.return_value = OrderResult(
        order_id="stop-order-1", client_order_id="stop-1", status="accepted",
        filled_qty=0, filled_avg_price=None, submitted_at=datetime.now(timezone.utc),
    )
    audit_path = tmp_path / "audit.jsonl"
    orchestrator = Orchestrator(
        broker=broker,
        signal_source=HoldSignalSource(),
        risk_manager=RiskManager(_thresholds()),
        audit_logger=AuditLogger(audit_path),
        watchlist=WATCHLIST,
        lookback_bars=200,
        protective_stop_fallback_pct=0.01,
    )
    orchestrator.run_cycle()

    # short position: fallback stop sits ABOVE current price (517.42 * 1.01)
    broker.submit_protective_stop.assert_called_once_with("MSFT", -20, Decimal("522.5942"))
    lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
    reattached = [line for line in lines if "PROTECTIVE_STOP_REATTACHED" in line]
    assert len(reattached) == 1
    assert '"severity": "CRITICAL"' in reattached[0]


def test_does_not_touch_position_that_already_has_a_stop(mocker, tmp_path):
    broker = _mock_broker(mocker)
    broker.get_positions.return_value = [
        Position(symbol="MSFT", qty=-20, avg_entry_price=Decimal("493.50"), unrealized_pl=Decimal("-478"), sector="tech"),
    ]
    broker.has_active_stop.return_value = True
    audit_path = tmp_path / "audit.jsonl"
    orchestrator = Orchestrator(
        broker=broker,
        signal_source=HoldSignalSource(),
        risk_manager=RiskManager(_thresholds()),
        audit_logger=AuditLogger(audit_path),
        watchlist=WATCHLIST,
        lookback_bars=200,
    )
    orchestrator.run_cycle()

    broker.submit_protective_stop.assert_not_called()


def test_live_mode_gate_rejects(monkeypatch, tmp_path):
    monkeypatch.setenv("ALPACA_API_KEY", "x")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "y")
    monkeypatch.setenv("TRADING_MODE", "live")

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
watchlist:
  - {symbol: AAPL, sector: tech}
bar_timeframe: "1H"
lookback_bars: 10
audit_log_path: "logs/audit.jsonl"
trend_ma_period: 5
volume_lookback: 5
volume_confirmation_multiplier: 1.2
min_level_touches: 2
level_touch_tolerance_pct: 0.003
max_risk_per_trade_pct: 0.01
max_position_value_pct: 0.10
daily_loss_cap_pct: 0.03
max_open_positions: 5
min_reward_risk_ratio: 2.0
correlation_basket_max: 2
slippage_max_pct: 0.003
market_edge_buffer_minutes: 5
kill_switch_consecutive_losses: 3
kill_switch_api_errors: 5
kill_switch_api_error_window_minutes: 15
protective_stop_fallback_pct: 0.01
""",
        encoding="utf-8",
    )

    with pytest.raises(Exception):
        Settings.from_env(config_path=config_path)
