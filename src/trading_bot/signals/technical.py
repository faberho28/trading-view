"""TechnicalSignal: swing-high/swing-low pivot detection -> support/resistance ->
breakout BUY/SELL/HOLD signal generation, per Requirement 2 of the spec.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from trading_bot.domain.models import Action, Bar, Signal

_DEFAULT_PIVOT_WINDOW = 3  # bars on each side required to confirm a swing pivot
_STOP_BUFFER_PCT = Decimal("0.01")
_TARGET_RR = Decimal("2")


class TechnicalSignal:
    """Support/resistance breakout strategy over a rolling window of 1H bars."""

    def __init__(self, pivot_window: int = _DEFAULT_PIVOT_WINDOW) -> None:
        self.pivot_window = pivot_window

    def generate(self, symbol: str, bars: list[Bar]) -> Signal:
        now = datetime.now(timezone.utc)
        min_bars = 2 * self.pivot_window + 2
        if len(bars) < min_bars:
            return self._hold(symbol, bars, now, "insufficient bars for pivot analysis")

        resistances, supports = self._find_pivots(bars)
        latest = bars[-1]

        if resistances and latest.close > resistances[-1]:
            level = resistances[-1]
            confidence = self._confidence(latest.close, level)
            stop = latest.close * (1 - _STOP_BUFFER_PCT)
            target = latest.close + (latest.close - stop) * _TARGET_RR
            return Signal(
                symbol=symbol,
                action=Action.BUY,
                confidence=confidence,
                reference_price=latest.close,
                stop_price=stop,
                target_price=target,
                rationale=f"close {latest.close} broke above resistance {level}",
                generated_at=now,
            )

        if supports and latest.close < supports[-1]:
            level = supports[-1]
            confidence = self._confidence(level, latest.close)
            stop = latest.close * (1 + _STOP_BUFFER_PCT)
            target = latest.close - (stop - latest.close) * _TARGET_RR
            return Signal(
                symbol=symbol,
                action=Action.SELL,
                confidence=confidence,
                reference_price=latest.close,
                stop_price=stop,
                target_price=target,
                rationale=f"close {latest.close} broke below support {level}",
                generated_at=now,
            )

        return self._hold(symbol, bars, now, "price within established S/R band")

    def _find_pivots(self, bars: list[Bar]) -> tuple[list[Decimal], list[Decimal]]:
        w = self.pivot_window
        resistances: list[Decimal] = []
        supports: list[Decimal] = []
        for i in range(w, len(bars) - w):
            window = bars[i - w : i + w + 1]
            candidate = bars[i]
            if candidate.high == max(b.high for b in window) and _is_strict_max(
                candidate.high, window, i - (i - w)
            ):
                resistances.append(candidate.high)
            if candidate.low == min(b.low for b in window) and _is_strict_min(
                candidate.low, window, i - (i - w)
            ):
                supports.append(candidate.low)
        return resistances, supports

    @staticmethod
    def _confidence(breakout_price: Decimal, level: Decimal) -> float:
        if level == 0:
            return 0.5
        raw = float((breakout_price - level) / level)
        return max(0.0, min(1.0, 0.5 + raw * 10))

    @staticmethod
    def _hold(symbol: str, bars: list[Bar], now: datetime, reason: str) -> Signal:
        ref = bars[-1].close if bars else Decimal("0")
        return Signal(
            symbol=symbol,
            action=Action.HOLD,
            confidence=0.0,
            reference_price=ref,
            stop_price=None,
            target_price=None,
            rationale=reason,
            generated_at=now,
        )


def _is_strict_max(value: Decimal, window: list[Bar], center_idx: int) -> bool:
    others = [b.high for idx, b in enumerate(window) if idx != center_idx]
    return all(value > o for o in others) if others else True


def _is_strict_min(value: Decimal, window: list[Bar], center_idx: int) -> bool:
    others = [b.low for idx, b in enumerate(window) if idx != center_idx]
    return all(value < o for o in others) if others else True
