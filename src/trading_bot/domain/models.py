"""Core domain models: frozen dataclasses and enums shared across ports and adapters.

Kept dependency-free (stdlib only) so the domain layer never leaks infrastructure
details into ports or the risk engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Action(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass(frozen=True)
class Bar:
    symbol: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


@dataclass(frozen=True)
class Account:
    equity: Decimal
    cash: Decimal
    buying_power: Decimal


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: int
    avg_entry_price: Decimal
    unrealized_pl: Decimal
    sector: str


@dataclass(frozen=True)
class MarketClock:
    is_open: bool
    next_open: datetime
    next_close: datetime
    now: datetime


@dataclass(frozen=True)
class Signal:
    symbol: str
    action: Action
    confidence: float
    reference_price: Decimal
    stop_price: Decimal | None
    target_price: Decimal | None
    rationale: str
    generated_at: datetime


@dataclass(frozen=True)
class TradeIntent:
    symbol: str
    side: Side
    qty: int
    limit_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    client_order_id: str
    signal: Signal


@dataclass(frozen=True)
class OrderResult:
    order_id: str
    client_order_id: str
    status: str
    filled_qty: int
    filled_avg_price: Decimal | None
    submitted_at: datetime


@dataclass(frozen=True)
class GuardrailResult:
    name: str
    passed: bool
    detail: str
    computed: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RiskContext:
    account: Account
    positions: list[Position]
    session_state: "SessionStateSnapshot"
    latest_price: Decimal
    clock: MarketClock


@dataclass(frozen=True)
class SessionStateSnapshot:
    """Immutable snapshot of mutable SessionState, passed to pure guardrails."""

    daily_pnl_pct: float
    consecutive_losses: int
    api_errors_in_window: int
    halted: bool


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    qty: int
    checks: list[GuardrailResult]
    rejection_reasons: list[str]
