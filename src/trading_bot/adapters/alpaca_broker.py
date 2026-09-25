"""AlpacaBrokerAdapter: implements BrokerPort against Alpaca's paper trading API.

Clients are injected via the constructor (never constructed here from env),
so tests substitute `MagicMock()` clients and never touch the network.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from trading_bot.domain.models import (
    Account,
    Bar,
    MarketClock,
    OrderResult,
    Position,
    TradeIntent,
)
from trading_bot.ports.broker import BrokerError


class AlpacaBrokerAdapter:
    def __init__(self, trading_client: Any, data_client: Any) -> None:
        self._trading_client = trading_client
        self._data_client = data_client

    def get_account(self) -> Account:
        try:
            acct = self._trading_client.get_account()
        except Exception as exc:  # noqa: BLE001 - SDK exceptions vary by version
            raise BrokerError(f"get_account failed: {exc}") from exc
        return Account(
            equity=Decimal(str(acct.equity)),
            cash=Decimal(str(acct.cash)),
            buying_power=Decimal(str(acct.buying_power)),
        )

    def get_positions(self) -> list[Position]:
        try:
            raw_positions = self._trading_client.get_all_positions()
        except Exception as exc:  # noqa: BLE001
            raise BrokerError(f"get_positions failed: {exc}") from exc
        return [
            Position(
                symbol=p.symbol,
                qty=int(float(p.qty)),
                avg_entry_price=Decimal(str(p.avg_entry_price)),
                unrealized_pl=Decimal(str(p.unrealized_pl)),
                sector=getattr(p, "sector", "unknown"),
            )
            for p in raw_positions
        ]

    def get_bars(self, symbol: str, limit: int) -> list[Bar]:
        from datetime import timedelta

        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        # Without an explicit `start`, Alpaca only returns bars from the
        # current session — `limit` alone does NOT reach back further in
        # time. ~6.5 trading hours/day means `limit` 1H bars need roughly
        # limit/6.5 trading days of history; the 2.2x multiplier plus a
        # 5-day floor comfortably covers weekends/holidays in that window.
        #
        # IMPORTANT: when `start` is set, Alpaca paginates forward from
        # `start` — passing `limit` here would return the EARLIEST `limit`
        # bars on/after `start`, not the most recent ones. Confirmed in
        # production: a narrow request returned bars stuck days in the
        # past instead of reaching "now". So we deliberately omit `limit`
        # from the request and slice the most recent `limit` bars ourselves
        # after fetching the full [start, now) range.
        lookback_days = max(5, int(limit / 6.5 * 2.2))
        start = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        request = StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Hour, start=start
        )
        try:
            response = self._data_client.get_stock_bars(request)
        except Exception as exc:  # noqa: BLE001
            raise BrokerError(f"get_bars({symbol}) failed: {exc}") from exc

        raw_bars = response[symbol] if hasattr(response, "__getitem__") else response.data[symbol]
        raw_bars = raw_bars[-limit:] if limit else raw_bars
        return [
            Bar(
                symbol=symbol,
                timestamp=b.timestamp,
                open=Decimal(str(b.open)),
                high=Decimal(str(b.high)),
                low=Decimal(str(b.low)),
                close=Decimal(str(b.close)),
                volume=int(b.volume),
            )
            for b in raw_bars
        ]

    def get_latest_price(self, symbol: str) -> Decimal:
        from alpaca.data.requests import StockLatestTradeRequest

        request = StockLatestTradeRequest(symbol_or_symbols=symbol)
        try:
            response = self._data_client.get_stock_latest_trade(request)
        except Exception as exc:  # noqa: BLE001
            raise BrokerError(f"get_latest_price({symbol}) failed: {exc}") from exc
        trade = response[symbol] if hasattr(response, "__getitem__") else response
        return Decimal(str(trade.price))

    def place_order(self, intent: TradeIntent) -> OrderResult:
        from decimal import ROUND_HALF_UP

        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, StopLossRequest, TakeProfitRequest

        def cents(price: Decimal) -> float:
            # SEC Rule 612 (sub-penny rule): orders on stocks priced >= $1
            # must be in whole-cent increments — confirmed in production:
            # Alpaca rejected an MSFT order over a $493.115 limit price.
            return float(price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

        side = OrderSide.BUY if intent.side.value == "BUY" else OrderSide.SELL
        # A bracket order attaches the stop-loss/take-profit legs the signal
        # already computed, so the position closes itself when either is
        # hit — the entry order alone previously left positions open
        # forever with no exit path (confirmed in production: the first 3
        # EXECUTED trades on 2026-09-16 had no way to ever close).
        request = LimitOrderRequest(
            symbol=intent.symbol,
            qty=intent.qty,
            side=side,
            time_in_force=TimeInForce.DAY,
            limit_price=cents(intent.limit_price),
            client_order_id=intent.client_order_id,
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=cents(intent.target_price)),
            stop_loss=StopLossRequest(stop_price=cents(intent.stop_price)),
        )
        try:
            order = self._trading_client.submit_order(request)
        except Exception as exc:  # noqa: BLE001
            raise BrokerError(f"place_order({intent.symbol}) failed: {exc}") from exc
        return OrderResult(
            order_id=str(order.id),
            client_order_id=intent.client_order_id,
            status=str(order.status),
            filled_qty=int(float(order.filled_qty or 0)),
            filled_avg_price=(
                Decimal(str(order.filled_avg_price)) if order.filled_avg_price else None
            ),
            submitted_at=order.submitted_at or datetime.now(timezone.utc),
        )

    def cancel_order(self, order_id: str) -> None:
        try:
            self._trading_client.cancel_order_by_id(order_id)
        except Exception as exc:  # noqa: BLE001
            raise BrokerError(f"cancel_order({order_id}) failed: {exc}") from exc

    def has_active_stop(self, symbol: str) -> bool:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        request = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
        try:
            orders = self._trading_client.get_orders(request)
        except Exception as exc:  # noqa: BLE001
            raise BrokerError(f"has_active_stop({symbol}) failed: {exc}") from exc
        return any("stop" in str(getattr(o, "order_type", "")).lower() for o in orders)

    def submit_protective_stop(self, symbol: str, qty: int, stop_price: Decimal) -> OrderResult:
        from decimal import ROUND_HALF_UP

        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import StopOrderRequest

        # qty carries the position's own sign (negative = short, per
        # get_positions()): closing a short means BUYing, closing a long
        # means SELLing.
        side = OrderSide.BUY if qty < 0 else OrderSide.SELL
        price = stop_price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        request = StopOrderRequest(
            symbol=symbol,
            qty=abs(qty),
            side=side,
            time_in_force=TimeInForce.DAY,
            stop_price=float(price),
        )
        try:
            order = self._trading_client.submit_order(request)
        except Exception as exc:  # noqa: BLE001
            raise BrokerError(f"submit_protective_stop({symbol}) failed: {exc}") from exc
        return OrderResult(
            order_id=str(order.id),
            client_order_id=str(order.client_order_id),
            status=str(order.status),
            filled_qty=int(float(order.filled_qty or 0)),
            filled_avg_price=(
                Decimal(str(order.filled_avg_price)) if order.filled_avg_price else None
            ),
            submitted_at=order.submitted_at or datetime.now(timezone.utc),
        )

    def is_market_open(self) -> MarketClock:
        try:
            clock = self._trading_client.get_clock()
        except Exception as exc:  # noqa: BLE001
            raise BrokerError(f"is_market_open failed: {exc}") from exc
        return MarketClock(
            is_open=bool(clock.is_open),
            next_open=clock.next_open,
            next_close=clock.next_close,
            now=clock.timestamp,
        )
