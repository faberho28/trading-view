"""The 9 guardrails from Requirement 3 of the spec.

Each `Guardrail.check()` is a pure function: no I/O, no clock reads (time
comes from `ctx.clock`), so every guardrail is trivially unit-testable with a
hand-built `RiskContext`. `RiskManager` (manager.py) composes and aggregates
all of them without short-circuiting, except for the kill switch.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from trading_bot.domain.models import GuardrailResult, RiskContext, Signal


class Guardrail(Protocol):
    name: str

    def check(self, signal: Signal, ctx: RiskContext, proposed_qty: int) -> GuardrailResult: ...


TECH_BASKET = {"AAPL", "MSFT", "NVDA", "GOOGL"}


class MaxRiskPerTradeGuardrail:
    name = "max_risk_per_trade"

    def __init__(self, max_risk_pct: float) -> None:
        self.max_risk_pct = Decimal(str(max_risk_pct))

    def check(self, signal: Signal, ctx: RiskContext, proposed_qty: int) -> GuardrailResult:
        if signal.stop_price is None:
            return GuardrailResult(self.name, False, "no stop price defined", {})
        stop_distance = abs(signal.reference_price - signal.stop_price)
        risked_capital = stop_distance * proposed_qty
        max_allowed = ctx.account.equity * self.max_risk_pct
        passed = risked_capital <= max_allowed
        return GuardrailResult(
            self.name,
            passed,
            f"risked_capital={risked_capital} max_allowed={max_allowed}",
            {"risked_capital": float(risked_capital), "max_allowed": float(max_allowed)},
        )


class PositionSizingCapGuardrail:
    """Also computes `proposed_qty`, which the RiskManager feeds forward to
    the remaining guardrails (per design.md).

    qty is the SMALLER of the risk-budget-based size (risk_budget /
    stop_distance) and the position-value-cap-based size (cap_budget /
    price). Sizing purely off risk budget is only safe when the stop
    distance is wide enough relative to price; a stop as tight as the risk
    percentage itself (e.g. both 1%) makes the risk-only qty approach the
    entire account, which the cap alone can then only reject outright
    rather than propose a smaller, still-valid trade.
    """

    name = "position_sizing_cap"

    def __init__(self, max_risk_pct: float, max_position_value_pct: float) -> None:
        self.max_risk_pct = Decimal(str(max_risk_pct))
        self.max_position_value_pct = Decimal(str(max_position_value_pct))

    def compute_qty(self, signal: Signal, ctx: RiskContext) -> int:
        if signal.stop_price is None or signal.reference_price <= 0:
            return 0
        stop_distance = abs(signal.reference_price - signal.stop_price)
        if stop_distance <= 0:
            return 0
        risk_budget = ctx.account.equity * self.max_risk_pct
        risk_based_qty = int(risk_budget / stop_distance)
        cap_budget = ctx.account.equity * self.max_position_value_pct
        cap_based_qty = int(cap_budget / signal.reference_price)
        return max(min(risk_based_qty, cap_based_qty), 0)

    def check(self, signal: Signal, ctx: RiskContext, proposed_qty: int) -> GuardrailResult:
        position_value = signal.reference_price * proposed_qty
        max_allowed = ctx.account.equity * self.max_position_value_pct
        passed = position_value <= max_allowed
        return GuardrailResult(
            self.name,
            passed,
            f"position_value={position_value} max_allowed={max_allowed}",
            {"position_value": float(position_value), "max_allowed": float(max_allowed)},
        )


class DailyLossCapGuardrail:
    name = "daily_loss_cap"

    def __init__(self, daily_loss_cap_pct: float) -> None:
        self.daily_loss_cap_pct = daily_loss_cap_pct

    def check(self, signal: Signal, ctx: RiskContext, proposed_qty: int) -> GuardrailResult:
        passed = ctx.session_state.daily_pnl_pct > -abs(self.daily_loss_cap_pct)
        return GuardrailResult(
            self.name,
            passed,
            f"daily_pnl_pct={ctx.session_state.daily_pnl_pct} cap={self.daily_loss_cap_pct}",
            {"daily_pnl_pct": ctx.session_state.daily_pnl_pct},
        )


class MaxOpenPositionsGuardrail:
    name = "max_open_positions"

    def __init__(self, max_open_positions: int) -> None:
        self.max_open_positions = max_open_positions

    def check(self, signal: Signal, ctx: RiskContext, proposed_qty: int) -> GuardrailResult:
        open_symbols = {p.symbol for p in ctx.positions}
        would_be_new = signal.symbol not in open_symbols
        resulting_count = len(open_symbols) + (1 if would_be_new else 0)
        passed = resulting_count <= self.max_open_positions
        return GuardrailResult(
            self.name,
            passed,
            f"resulting_count={resulting_count} max={self.max_open_positions}",
            {"resulting_count": resulting_count},
        )


class MinRewardRiskGuardrail:
    name = "min_reward_risk_ratio"

    def __init__(self, min_rr: float) -> None:
        self.min_rr = Decimal(str(min_rr))

    def check(self, signal: Signal, ctx: RiskContext, proposed_qty: int) -> GuardrailResult:
        if signal.stop_price is None or signal.target_price is None:
            return GuardrailResult(self.name, False, "missing stop/target price", {})
        risk = abs(signal.reference_price - signal.stop_price)
        reward = abs(signal.target_price - signal.reference_price)
        if risk == 0:
            return GuardrailResult(self.name, False, "zero risk distance", {})
        rr = reward / risk
        passed = rr >= self.min_rr
        return GuardrailResult(
            self.name, passed, f"rr={rr} min={self.min_rr}", {"reward_risk_ratio": float(rr)}
        )


class CorrelationCapGuardrail:
    name = "correlation_cap"

    def __init__(self, basket_max: int) -> None:
        self.basket_max = basket_max

    def check(self, signal: Signal, ctx: RiskContext, proposed_qty: int) -> GuardrailResult:
        sector = self._sector_for(signal.symbol, ctx)
        same_basket_open = [
            p for p in ctx.positions if self._sector_for(p.symbol, ctx) == sector
        ]
        already_open_this_symbol = any(p.symbol == signal.symbol for p in ctx.positions)
        resulting_count = len(same_basket_open) + (0 if already_open_this_symbol else 1)
        passed = resulting_count <= self.basket_max
        return GuardrailResult(
            self.name,
            passed,
            f"sector={sector} resulting_count={resulting_count} max={self.basket_max}",
            {"resulting_count": resulting_count, "sector": sector},
        )

    @staticmethod
    def _sector_for(symbol: str, ctx: RiskContext) -> str:
        for p in ctx.positions:
            if p.symbol == symbol:
                return p.sector
        return "tech" if symbol in TECH_BASKET else "index"


class MarketHoursWindowGuardrail:
    name = "market_hours_window"

    def __init__(self, edge_buffer_minutes: int) -> None:
        self.edge_buffer_minutes = edge_buffer_minutes

    def check(self, signal: Signal, ctx: RiskContext, proposed_qty: int) -> GuardrailResult:
        clock = ctx.clock
        if not clock.is_open:
            return GuardrailResult(self.name, False, "market closed", {})
        buffer_seconds = self.edge_buffer_minutes * 60
        # clock.next_open is always the *next* scheduled open. While the
        # market is open that's tomorrow's open, not today's, so
        # (now - next_open) is a large negative number — there is no
        # "most recent open" field available to compute a real since_open
        # value against. Only the close-side buffer is checkable here.
        until_close = (clock.next_close - clock.now).total_seconds()
        passed = until_close > buffer_seconds
        return GuardrailResult(
            self.name,
            passed,
            f"until_close={until_close}s buffer={buffer_seconds}s",
            {"until_close_s": until_close},
        )


class SlippageGuardGuardrail:
    name = "slippage_guard"

    def __init__(self, max_slippage_pct: float) -> None:
        self.max_slippage_pct = Decimal(str(max_slippage_pct))

    def check(self, signal: Signal, ctx: RiskContext, proposed_qty: int) -> GuardrailResult:
        if signal.reference_price == 0:
            return GuardrailResult(self.name, False, "zero reference price", {})
        drift = abs(ctx.latest_price - signal.reference_price) / signal.reference_price
        passed = drift <= self.max_slippage_pct
        return GuardrailResult(
            self.name,
            passed,
            f"drift={drift} max={self.max_slippage_pct}",
            {"drift_pct": float(drift)},
        )


class KillSwitchGuardrail:
    """Guardrail 9. When active, `RiskManager.evaluate` short-circuits to
    reject before evaluating the other 8 (see manager.py)."""

    name = "kill_switch"

    def check(self, signal: Signal, ctx: RiskContext, proposed_qty: int) -> GuardrailResult:
        passed = not ctx.session_state.halted
        return GuardrailResult(
            self.name,
            passed,
            "halted" if ctx.session_state.halted else "not halted",
            {"halted": ctx.session_state.halted},
        )
