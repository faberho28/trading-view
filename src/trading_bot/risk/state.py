"""SessionState: mutable, in-memory, session-scoped state feeding guardrail 9
(kill switch) and guardrail 3 (daily loss cap).

Deliberately in-memory only for this slice (see design.md rollout note): a
persistence layer is out of scope, and the halt flag must NOT auto-clear on
restart, new day, or next cycle — only an explicit manual reset clears it.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone

from trading_bot.domain.models import SessionStateSnapshot


class SessionState:
    def __init__(self) -> None:
        self._daily_pnl_pct: float = 0.0
        self._consecutive_losses: int = 0
        self._api_error_timestamps: deque[datetime] = deque()
        self._halted: bool = False
        self._halt_reason: str | None = None

    def record_trade_result(self, pnl: float) -> None:
        """Update consecutive-loss streak from a closed trade's realized PnL."""
        if pnl < 0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0
        self._maybe_trigger_kill_switch(
            consecutive_loss_threshold=3, api_error_threshold=5, window_minutes=15
        )

    def record_api_error(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        self._api_error_timestamps.append(now)
        self._maybe_trigger_kill_switch(
            consecutive_loss_threshold=3, api_error_threshold=5, window_minutes=15
        )

    def update_daily_pnl_pct(self, pnl_pct: float, daily_loss_cap_pct: float) -> None:
        self._daily_pnl_pct = pnl_pct
        if pnl_pct <= -abs(daily_loss_cap_pct):
            self._trigger_halt("daily_loss_cap_hit")

    def api_errors_in_window(self, window_minutes: int, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=window_minutes)
        while self._api_error_timestamps and self._api_error_timestamps[0] < cutoff:
            self._api_error_timestamps.popleft()
        return len(self._api_error_timestamps)

    def _maybe_trigger_kill_switch(
        self,
        consecutive_loss_threshold: int,
        api_error_threshold: int,
        window_minutes: int,
    ) -> None:
        if self._consecutive_losses >= consecutive_loss_threshold:
            self._trigger_halt("consecutive_losses")
        if self.api_errors_in_window(window_minutes) > api_error_threshold:
            self._trigger_halt("api_error_threshold")

    def _trigger_halt(self, reason: str) -> None:
        self._halted = True
        self._halt_reason = reason

    def manual_reset(self) -> None:
        """The only way to clear the halt flag. Must be called explicitly by a human."""
        self._halted = False
        self._halt_reason = None
        self._consecutive_losses = 0
        self._api_error_timestamps.clear()

    def snapshot(self, window_minutes: int = 15) -> SessionStateSnapshot:
        return SessionStateSnapshot(
            daily_pnl_pct=self._daily_pnl_pct,
            consecutive_losses=self._consecutive_losses,
            api_errors_in_window=self.api_errors_in_window(window_minutes),
            halted=self._halted,
        )

    @property
    def halted(self) -> bool:
        return self._halted

    @property
    def halt_reason(self) -> str | None:
        return self._halt_reason
