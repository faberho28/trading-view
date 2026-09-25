"""BrokerPort: the hexagonal boundary between the core and any broker implementation.

Defined as a `typing.Protocol` (structural typing) so test doubles need no
inheritance and adapters stay fully decoupled from the core.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from trading_bot.domain.models import (
    Account,
    MarketClock,
    OrderResult,
    Position,
    TradeIntent,
)
from trading_bot.domain.models import Bar


class BrokerPort(Protocol):
    def get_account(self) -> Account: ...

    def get_positions(self) -> list[Position]: ...

    def get_bars(self, symbol: str, limit: int) -> list[Bar]: ...

    def get_latest_price(self, symbol: str) -> Decimal: ...

    def place_order(self, intent: TradeIntent) -> OrderResult: ...

    def cancel_order(self, order_id: str) -> None: ...

    def is_market_open(self) -> MarketClock: ...

    def has_active_stop(self, symbol: str) -> bool: ...

    def submit_protective_stop(self, symbol: str, qty: int, stop_price: Decimal) -> OrderResult: ...


class BrokerError(Exception):
    """Raised by adapters when the underlying broker SDK call fails.

    The orchestrator catches this per-symbol so a single transient failure
    never aborts the rest of the cycle.
    """
