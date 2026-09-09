from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from trading_bot.domain.models import (
    Account,
    Bar,
    MarketClock,
    Position,
    RiskContext,
    SessionStateSnapshot,
    Signal,
    Action,
)


@pytest.fixture(autouse=True)
def _clear_alpaca_env(monkeypatch):
    """Belt-and-braces guard: no test should ever depend on real credentials."""
    for key in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "TRADING_MODE"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def market_open_clock() -> MarketClock:
    now = datetime(2026, 9, 8, 15, 0, tzinfo=timezone.utc)  # well within market hours
    # next_open mirrors real Alpaca semantics: while the market is open,
    # it's the *next* scheduled open (tomorrow's), never a past timestamp.
    return MarketClock(
        is_open=True,
        next_open=now + timedelta(hours=19),
        next_close=now + timedelta(hours=5),
        now=now,
    )


@pytest.fixture
def sample_account() -> Account:
    return Account(equity=Decimal("100000"), cash=Decimal("50000"), buying_power=Decimal("100000"))


@pytest.fixture
def no_positions() -> list[Position]:
    return []


@pytest.fixture
def passing_session_snapshot() -> SessionStateSnapshot:
    return SessionStateSnapshot(
        daily_pnl_pct=0.0, consecutive_losses=0, api_errors_in_window=0, halted=False
    )


@pytest.fixture
def halted_session_snapshot() -> SessionStateSnapshot:
    return SessionStateSnapshot(
        daily_pnl_pct=0.0, consecutive_losses=3, api_errors_in_window=0, halted=True
    )


@pytest.fixture
def buy_signal() -> Signal:
    return Signal(
        symbol="AAPL",
        action=Action.BUY,
        confidence=0.8,
        reference_price=Decimal("100"),
        stop_price=Decimal("98"),
        target_price=Decimal("104"),
        rationale="test breakout",
        generated_at=datetime.now(timezone.utc),
    )


def make_ctx(
    account,
    positions,
    session_snapshot,
    latest_price,
    clock,
) -> RiskContext:
    return RiskContext(
        account=account,
        positions=positions,
        session_state=session_snapshot,
        latest_price=latest_price,
        clock=clock,
    )


@pytest.fixture
def passing_ctx(sample_account, no_positions, passing_session_snapshot, market_open_clock):
    return make_ctx(
        sample_account, no_positions, passing_session_snapshot, Decimal("100"), market_open_clock
    )


def make_bars(symbol: str, closes: list[float]) -> list[Bar]:
    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    bars = []
    for i, close in enumerate(closes):
        price = Decimal(str(close))
        bars.append(
            Bar(
                symbol=symbol,
                timestamp=base + timedelta(hours=i),
                open=price,
                high=price * Decimal("1.001"),
                low=price * Decimal("0.999"),
                close=price,
                volume=1000,
            )
        )
    return bars
