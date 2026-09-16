from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from trading_bot.adapters.alpaca_broker import AlpacaBrokerAdapter
from trading_bot.domain.models import Side, Signal, Action, TradeIntent


def test_place_order_rounds_limit_price_to_whole_cents(mocker):
    """Regression for the 2026-09-16 production incident: Alpaca rejected
    a real MSFT order with error 'invalid limit_price 493.115. sub-penny
    increment does not fulfill minimum pricing criteria' (SEC Rule 612 —
    stocks >= $1 must be quoted in whole-cent increments). limit_price
    came straight from a real-time trade price, which can carry more than
    2 decimal places."""
    trading_client = mocker.Mock()
    trading_client.submit_order.return_value = mocker.Mock(
        id="order-1",
        status="accepted",
        filled_qty="0",
        filled_avg_price=None,
        submitted_at=datetime.now(timezone.utc),
    )
    adapter = AlpacaBrokerAdapter(trading_client, mocker.Mock())
    signal = Signal(
        symbol="MSFT",
        action=Action.SELL,
        confidence=0.5,
        reference_price=Decimal("493.17"),
        stop_price=Decimal("498.10"),
        target_price=Decimal("483.31"),
        rationale="test sub-penny",
        generated_at=datetime.now(timezone.utc),
    )
    intent = TradeIntent(
        symbol="MSFT",
        side=Side.SELL,
        qty=20,
        limit_price=Decimal("493.115"),  # the exact price Alpaca rejected in production
        stop_price=Decimal("498.10"),
        target_price=Decimal("483.31"),
        client_order_id="MSFT-123",
        signal=signal,
    )

    adapter.place_order(intent)

    submitted_request = trading_client.submit_order.call_args[0][0]
    assert submitted_request.limit_price == 493.12  # ROUND_HALF_UP from .115
