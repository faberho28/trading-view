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


def test_place_order_attaches_bracket_stop_loss_and_take_profit(mocker):
    """Without a bracket order, a filled entry never closes on its own —
    confirmed in production: the first 3 EXECUTED trades on 2026-09-16
    (AAPL, NVDA, GOOGL) had no stop-loss or take-profit attached and would
    have stayed open indefinitely. The signal already computes stop_price
    and target_price; they must actually reach the order."""
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
        symbol="AAPL",
        action=Action.BUY,
        confidence=0.5,
        reference_price=Decimal("334.12"),
        stop_price=Decimal("330.78"),
        target_price=Decimal("340.80"),
        rationale="test bracket",
        generated_at=datetime.now(timezone.utc),
    )
    intent = TradeIntent(
        symbol="AAPL",
        side=Side.BUY,
        qty=29,
        limit_price=Decimal("334.12"),
        stop_price=Decimal("330.78"),
        target_price=Decimal("340.80"),
        client_order_id="AAPL-123",
        signal=signal,
    )

    adapter.place_order(intent)

    submitted_request = trading_client.submit_order.call_args[0][0]
    assert submitted_request.order_class.value == "bracket"
    assert submitted_request.stop_loss.stop_price == 330.78
    assert submitted_request.take_profit.limit_price == 340.80


def test_has_active_stop_counts_held_bracket_legs_as_protected(mocker):
    """Regression for the 2026-09-25 production incident: querying only
    status=OPEN missed a freshly-opened bracket's stop-loss leg sitting in
    Alpaca's transitional 'held' status moments after the entry filled,
    producing a false 'unprotected' positive that would have fired a
    redundant protective stop on a perfectly healthy position."""
    trading_client = mocker.Mock()
    held_stop_leg = mocker.Mock(order_type=mocker.Mock(__str__=lambda self: "OrderType.STOP"), status=mocker.Mock(__str__=lambda self: "OrderStatus.HELD"))
    trading_client.get_orders.return_value = [held_stop_leg]
    adapter = AlpacaBrokerAdapter(trading_client, mocker.Mock())

    assert adapter.has_active_stop("MSFT") is True


def test_has_active_stop_ignores_canceled_stop_orders(mocker):
    trading_client = mocker.Mock()
    canceled_stop = mocker.Mock(order_type=mocker.Mock(__str__=lambda self: "OrderType.STOP"), status=mocker.Mock(__str__=lambda self: "OrderStatus.CANCELED"))
    trading_client.get_orders.return_value = [canceled_stop]
    adapter = AlpacaBrokerAdapter(trading_client, mocker.Mock())

    assert adapter.has_active_stop("MSFT") is False
