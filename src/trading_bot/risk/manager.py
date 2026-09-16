"""RiskManager: aggregates all 9 guardrails per Requirement 3.

All guardrails run (no short-circuit) so the audit log reflects every
verdict, EXCEPT when the kill switch is active, in which case `evaluate`
short-circuits immediately to a rejection with only `kill_switch_active`.
"""

from __future__ import annotations

from trading_bot.domain.models import RiskContext, RiskDecision, Signal
from trading_bot.risk.guardrails import (
    CorrelationCapGuardrail,
    DailyLossCapGuardrail,
    Guardrail,
    KillSwitchGuardrail,
    MarketHoursWindowGuardrail,
    MaxOpenPositionsGuardrail,
    MaxRiskPerTradeGuardrail,
    MinRewardRiskGuardrail,
    NoDuplicatePositionGuardrail,
    PositionSizingCapGuardrail,
    SlippageGuardGuardrail,
)
from trading_bot.config import RiskThresholds


class RiskManager:
    def __init__(self, thresholds: RiskThresholds) -> None:
        self.thresholds = thresholds
        self.kill_switch = KillSwitchGuardrail()
        self.position_sizing = PositionSizingCapGuardrail(
            thresholds.max_risk_per_trade_pct, thresholds.max_position_value_pct
        )
        self.other_guardrails: list[Guardrail] = [
            NoDuplicatePositionGuardrail(),
            MaxRiskPerTradeGuardrail(thresholds.max_risk_per_trade_pct),
            self.position_sizing,
            DailyLossCapGuardrail(thresholds.daily_loss_cap_pct),
            MaxOpenPositionsGuardrail(thresholds.max_open_positions),
            MinRewardRiskGuardrail(thresholds.min_reward_risk_ratio),
            CorrelationCapGuardrail(thresholds.correlation_basket_max),
            MarketHoursWindowGuardrail(thresholds.market_edge_buffer_minutes),
            SlippageGuardGuardrail(thresholds.slippage_max_pct),
        ]

    def evaluate(self, signal: Signal, ctx: RiskContext) -> RiskDecision:
        kill_switch_result = self.kill_switch.check(signal, ctx, proposed_qty=0)
        if not kill_switch_result.passed:
            return RiskDecision(
                approved=False,
                qty=0,
                checks=[kill_switch_result],
                rejection_reasons=["kill_switch_active"],
            )

        proposed_qty = self.position_sizing.compute_qty(signal, ctx)

        checks = [kill_switch_result]
        rejection_reasons: list[str] = []
        for guardrail in self.other_guardrails:
            result = guardrail.check(signal, ctx, proposed_qty)
            checks.append(result)
            if not result.passed:
                rejection_reasons.append(result.name)

        approved = len(rejection_reasons) == 0
        return RiskDecision(
            approved=approved,
            qty=proposed_qty if approved else 0,
            checks=checks,
            rejection_reasons=rejection_reasons,
        )
