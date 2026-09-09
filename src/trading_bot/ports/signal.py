"""SignalPort: the hexagonal boundary for pluggable trading-signal strategies."""

from __future__ import annotations

from typing import Protocol

from trading_bot.domain.models import Bar, Signal


class SignalPort(Protocol):
    def generate(self, symbol: str, bars: list[Bar]) -> Signal: ...
