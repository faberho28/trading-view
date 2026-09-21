"""TechnicalSignal: swing-high/swing-low pivot detection -> support/resistance ->
breakout BUY/SELL/HOLD signal generation, per Requirement 2 of the spec.

Added 2026-09-21: a raw pivot breakout alone fires on any noise the moment
price crosses the single most recent swing high/low. Three confirmation
filters were added on top, per user decision after reviewing a week of
live signals that "entered mid-move" with no sense of trend or conviction:

1. Level strength — only a price level that recurred at least
   `min_level_touches` times (within `level_touch_tolerance_pct`) counts as
   real support/resistance, not a one-off wiggle.
2. Trend agreement — a BUY breakout must occur above the trend moving
   average, a SELL breakout below it. Counter-trend breakouts are the ones
   most likely to be noise/false starts.
3. Volume confirmation — the breakout bar's volume must exceed its trailing
   baseline by `volume_confirmation_multiplier`. A "breakout" on
   below-average volume rarely holds.

All three are independently checkable and testable against real data,
unlike subjective classical chart-pattern recognition (head-and-shoulders,
flags, etc.), which was deliberately NOT implemented here — see
sdd/real-trading-bot/strategy-confirmation-filters in project memory for
the reasoning.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from trading_bot.domain.models import Action, Bar, Signal

_DEFAULT_PIVOT_WINDOW = 3  # bars on each side required to confirm a swing pivot
_STOP_BUFFER_PCT = Decimal("0.01")
_TARGET_RR = Decimal("2")


class TechnicalSignal:
    """Support/resistance breakout strategy over a rolling window of 1H bars,
    confirmed by level strength, trend agreement, and volume."""

    def __init__(
        self,
        pivot_window: int = _DEFAULT_PIVOT_WINDOW,
        trend_ma_period: int = 50,
        volume_lookback: int = 20,
        volume_confirmation_multiplier: float = 1.2,
        min_level_touches: int = 2,
        level_touch_tolerance_pct: float = 0.003,
    ) -> None:
        self.pivot_window = pivot_window
        self.trend_ma_period = trend_ma_period
        self.volume_lookback = volume_lookback
        self.volume_confirmation_multiplier = Decimal(str(volume_confirmation_multiplier))
        self.min_level_touches = min_level_touches
        self.level_touch_tolerance_pct = Decimal(str(level_touch_tolerance_pct))

    def generate(self, symbol: str, bars: list[Bar]) -> Signal:
        now = datetime.now(timezone.utc)
        min_bars = max(2 * self.pivot_window + 2, self.trend_ma_period, self.volume_lookback + 1)
        if len(bars) < min_bars:
            return self._hold(symbol, bars, now, "insufficient bars for pivot/trend/volume analysis")

        raw_resistances, raw_supports = self._find_pivots(bars)
        resistances = self._strong_levels(raw_resistances)
        supports = self._strong_levels(raw_supports)
        latest = bars[-1]
        trend_ma = self._moving_average(bars, self.trend_ma_period)

        if resistances and latest.close > resistances[-1]:
            level = resistances[-1]
            if latest.close <= trend_ma:
                return self._hold(
                    symbol, bars, now,
                    f"resistance {level} broken but close {latest.close} is below trend MA {trend_ma} - counter-trend, skipped",
                )
            if not self._volume_confirmed(bars):
                return self._hold(
                    symbol, bars, now,
                    f"resistance {level} broken but breakout volume did not confirm - skipped",
                )
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
                rationale=(
                    f"close {latest.close} broke above resistance {level} "
                    f"(tested >={self.min_level_touches}x), above trend MA {trend_ma}, volume confirmed"
                ),
                generated_at=now,
            )

        if supports and latest.close < supports[-1]:
            level = supports[-1]
            if latest.close >= trend_ma:
                return self._hold(
                    symbol, bars, now,
                    f"support {level} broken but close {latest.close} is above trend MA {trend_ma} - counter-trend, skipped",
                )
            if not self._volume_confirmed(bars):
                return self._hold(
                    symbol, bars, now,
                    f"support {level} broken but breakout volume did not confirm - skipped",
                )
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
                rationale=(
                    f"close {latest.close} broke below support {level} "
                    f"(tested >={self.min_level_touches}x), below trend MA {trend_ma}, volume confirmed"
                ),
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

    def _strong_levels(self, prices: list[Decimal]) -> list[Decimal]:
        """Group nearby pivot prices (within level_touch_tolerance_pct) into
        one level and keep only levels that recurred min_level_touches+
        times. A single unconfirmed wiggle is not real support/resistance.
        """
        if not prices:
            return []
        clusters: list[list[Decimal]] = []
        for price in prices:
            placed = False
            for cluster in clusters:
                ref = cluster[-1]
                if ref != 0 and abs(price - ref) / ref <= self.level_touch_tolerance_pct:
                    cluster.append(price)
                    placed = True
                    break
            if not placed:
                clusters.append([price])
        # keep chronological order (clusters list is already in first-seen
        # order); represent each qualifying cluster by its most recent touch
        return [cluster[-1] for cluster in clusters if len(cluster) >= self.min_level_touches]

    @staticmethod
    def _moving_average(bars: list[Bar], period: int) -> Decimal:
        window = bars[-period:]
        return sum((b.close for b in window), Decimal("0")) / len(window)

    def _volume_confirmed(self, bars: list[Bar]) -> bool:
        baseline_window = bars[-(self.volume_lookback + 1) : -1]
        if not baseline_window:
            return False
        baseline_avg = Decimal(sum(b.volume for b in baseline_window)) / len(baseline_window)
        if baseline_avg <= 0:
            return False
        return Decimal(bars[-1].volume) > baseline_avg * self.volume_confirmation_multiplier

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
