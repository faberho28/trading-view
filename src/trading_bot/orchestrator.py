"""Orchestrator: the composition point for BrokerPort, SignalPort, and
RiskManager (Requirement 4). No direct imports of concrete adapters here —
dependency injection only, proven by dependency-substitution tests.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from trading_bot.audit import AuditLogger
from trading_bot.domain.models import (
    Action,
    Position,
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
        protective_stop_fallback_pct: float = 0.01,
    ) -> None:
        self.broker = broker
        self.signal_source = signal_source
        self.risk_manager = risk_manager
        self.audit_logger = audit_logger
        self.watchlist = watchlist
        self.lookback_bars = lookback_bars
        self.session_state = session_state or SessionState()
        self.daily_loss_cap_pct = daily_loss_cap_pct
        self.protective_stop_fallback_pct = Decimal(str(protective_stop_fallback_pct))

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

        self._ensure_positions_protected(cycle_id, positions)

        for symbol in self.watchlist:
            self._process_symbol(cycle_id, symbol, account, positions, clock)

    def _ensure_positions_protected(self, cycle_id, positions) -> None:
        """Safety net added 2026-09-25 after a production incident: 3 open
        positions (GOOGL, MSFT, SPY) sat for about a week with NO active
        stop-loss order — the original bracket leg had been cancelled
        (most likely swept up by hand while clearing unrelated stuck
        orders) and nothing noticed, so they kept losing well past the
        1% the strategy intends to risk. Every cycle now checks each open
        position for a live stop order and re-attaches one immediately if
        missing.
        """
        for position in positions:
            try:
                if self.broker.has_active_stop(position.symbol):
                    continue
                # Anchor to the CURRENT price, not avg_entry_price: once a
                # position has already drifted past the intended stop
                # distance from entry (exactly the failure this guards
                # against), an entry-based stop price ends up on the wrong
                # side of the market and Alpaca rejects it outright
                # ("stop price must be less/greater than current price").
                # Confirmed in production: GOOGL/MSFT/SPY had all moved
                # well past their 1%-from-entry level by the time this ran.
                latest_price = self.broker.get_latest_price(position.symbol)
                stop_price = self._fallback_stop_price(position, latest_price)
                order_result = self.broker.submit_protective_stop(
                    position.symbol, position.qty, stop_price
                )
                self.audit_logger.write(
                    {
                        "cycle_id": cycle_id,
                        "symbol": position.symbol,
                        "final_action": "PROTECTIVE_STOP_REATTACHED",
                        "severity": "CRITICAL",
                        "detail": "open position had no active stop-loss order; re-attached one",
                        "qty": position.qty,
                        "avg_entry_price": str(position.avg_entry_price),
                        "new_stop_price": str(stop_price),
                        "order_id": order_result.order_id,
                    }
                )
            except BrokerError as exc:
                self.session_state.record_api_error()
                self.audit_logger.write(
                    {
                        "cycle_id": cycle_id,
                        "symbol": position.symbol,
                        "final_action": "ERROR",
                        "error": f"position protection check failed: {exc}",
                    }
                )

    def _fallback_stop_price(self, position, latest_price: Decimal) -> Decimal:
        if position.qty < 0:  # short: protective stop sits above current price
            return latest_price * (1 + self.protective_stop_fallback_pct)
        return latest_price * (1 - self.protective_stop_fallback_pct)  # long: below

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
            # `positions` is fetched once per cycle and shared by reference
            # across every symbol in self.watchlist (see run_cycle). Without
            # this, guardrails that count open positions (correlation_cap,
            # max_open_positions, no_duplicate_position) only see the state
            # from BEFORE the cycle started — confirmed in production:
            # AAPL, MSFT, and NVDA all broke out in the same or adjacent
            # cycles and piled up as 3 simultaneous tech positions despite
            # correlation_basket_max=2, because neither saw the other's
            # fill from moments earlier in the same loop.
            positions.append(
                Position(
                    symbol=symbol,
                    qty=decision.qty,
                    avg_entry_price=latest_price,
                    unrealized_pl=Decimal("0"),
                    sector="unknown",
                )
            )

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
