"""Orchestrator: the composition point for BrokerPort, SignalPort, and
RiskManager (Requirement 4). No direct imports of concrete adapters here —
dependency injection only, proven by dependency-substitution tests.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone

from trading_bot.audit import AuditLogger
from trading_bot.domain.models import (
    Action,
    RiskContext,
    Side,
    TradeIntent,
)
from trading_bot.ports.broker import BrokerError, BrokerPort
from trading_bot.ports.signal import SignalPort
from trading_bot.risk.manager import RiskManager
from trading_bot.risk.state import SessionState


class Orchestrator:
    def __init__(
        self,
        broker: BrokerPort,
        signal_source: SignalPort,
        risk_manager: RiskManager,
        audit_logger: AuditLogger,
        watchlist: list[str],
        lookback_bars: int,
        session_state: SessionState | None = None,
        daily_loss_cap_pct: float = 0.03,
    ) -> None:
        self.broker = broker
        self.signal_source = signal_source
        self.risk_manager = risk_manager
        self.audit_logger = audit_logger
        self.watchlist = watchlist
        self.lookback_bars = lookback_bars
        self.session_state = session_state or SessionState()
        self.daily_loss_cap_pct = daily_loss_cap_pct

    def run_cycle(self) -> None:
        cycle_id = str(uuid.uuid4())

        if self.session_state.halted:
            for symbol in self.watchlist:
                self.audit_logger.write(
                    {
                        "cycle_id": cycle_id,
                        "symbol": symbol,
                        "mode": "paper",
                        "action": "HALTED",
                        "final_action": "REJECTED",
                        "halt_reason": self.session_state.halt_reason,
                    }
                )
            return

        try:
            clock = self.broker.is_market_open()
        except BrokerError as exc:
            self.session_state.record_api_error()
            for symbol in self.watchlist:
                self.audit_logger.write(
                    {"cycle_id": cycle_id, "symbol": symbol, "final_action": "ERROR", "error": str(exc)}
                )
            return

        # Alpaca's clock.next_open is always the *next* scheduled open. While
        # the market is open, that's tomorrow's open, not today's — so
        # `now - next_open` is a large negative timedelta and would always
        # be "< edge_buffer", incorrectly rejecting every cycle all day.
        # There is no "most recent open" field to compare against, so we
        # only guard the close-side edge here; at this bot's 1H cadence the
        # chance of a cycle landing inside the first 5 minutes after open is
        # already negligible (it would require the hourly wake to coincide
        # almost exactly with the session start).
        edge_buffer = timedelta(minutes=5)
        if not clock.is_open or (clock.next_close - clock.now) < edge_buffer:
            for symbol in self.watchlist:
                self.audit_logger.write(
                    {"cycle_id": cycle_id, "symbol": symbol, "final_action": "SKIPPED", "reason": "market_hours"}
                )
            return

        try:
            account = self.broker.get_account()
            positions = self.broker.get_positions()
        except BrokerError as exc:
            self.session_state.record_api_error()
            for symbol in self.watchlist:
                self.audit_logger.write(
                    {"cycle_id": cycle_id, "symbol": symbol, "final_action": "ERROR", "error": str(exc)}
                )
            return

        for symbol in self.watchlist:
            self._process_symbol(cycle_id, symbol, account, positions, clock)

    def _process_symbol(self, cycle_id, symbol, account, positions, clock) -> None:
        record: dict = {"cycle_id": cycle_id, "symbol": symbol, "mode": "paper"}
        try:
            bars = self.broker.get_bars(symbol, self.lookback_bars)
            signal = self.signal_source.generate(symbol, bars)
            record.update(
                {
                    "signal_source": "technical_sr_breakout",
                    "signal_action": signal.action.value,
                    "confidence": signal.confidence,
                    "reference_price": str(signal.reference_price),
                    "rationale": signal.rationale,
                    "bars_available": len(bars),
                }
            )

            if signal.action == Action.HOLD:
                record["final_action"] = "HOLD"
                self.audit_logger.write(record)
                return

            latest_price = self.broker.get_latest_price(symbol)
            ctx = RiskContext(
                account=account,
                positions=positions,
                session_state=self.session_state.snapshot(),
                latest_price=latest_price,
                clock=clock,
            )
            decision = self.risk_manager.evaluate(signal, ctx)
            record["guardrail_results"] = [
                {"name": c.name, "passed": c.passed, "detail": c.detail, "computed": c.computed}
                for c in decision.checks
            ]
            record["rejection_reasons"] = decision.rejection_reasons
            record["proposed_qty"] = decision.qty

            if not decision.approved:
                record["final_action"] = "REJECTED"
                record["order_id"] = None
                self.audit_logger.write(record)
                return

            bar_close_epoch = int(bars[-1].timestamp.timestamp()) if bars else int(time.time())
            client_order_id = f"{symbol}-{bar_close_epoch}"
            side = Side.BUY if signal.action == Action.BUY else Side.SELL
            intent = TradeIntent(
                symbol=symbol,
                side=side,
                qty=decision.qty,
                limit_price=latest_price,
                stop_price=signal.stop_price,
                target_price=signal.target_price,
                client_order_id=client_order_id,
                signal=signal,
            )
            order_result = self.broker.place_order(intent)
            record["final_action"] = "EXECUTED"
            record["order_id"] = order_result.order_id
            self.audit_logger.write(record)

        except BrokerError as exc:
            self.session_state.record_api_error()
            record["final_action"] = "ERROR"
            record["error"] = str(exc)
            self.audit_logger.write(record)

    def run_forever(self) -> None:
        while True:
            self._sleep_until_next_hour_boundary()
            self.run_cycle()

    @staticmethod
    def _sleep_until_next_hour_boundary(extra_seconds: int = 30) -> None:
        now = datetime.now(timezone.utc)
        next_hour = (now.replace(minute=0, second=0, microsecond=0)) + timedelta(hours=1)
        target = next_hour + timedelta(seconds=extra_seconds)
        sleep_seconds = (target - now).total_seconds()
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
