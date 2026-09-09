"""Settings composition: env secrets (via python-dotenv) + config.yaml thresholds.

`Settings.from_env()` is the single place that reads the process environment.
Every other module receives configuration via constructor injection, so tests
never need to touch `os.environ`.
"""

from __future__ import annotations

from pathlib import Path

import os
import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator


class WatchlistEntry(BaseModel):
    symbol: str
    sector: str


class RiskThresholds(BaseModel):
    max_risk_per_trade_pct: float
    max_position_value_pct: float
    daily_loss_cap_pct: float
    max_open_positions: int
    min_reward_risk_ratio: float
    correlation_basket_max: int
    slippage_max_pct: float
    market_edge_buffer_minutes: int
    kill_switch_consecutive_losses: int
    kill_switch_api_errors: int
    kill_switch_api_error_window_minutes: int


class AppConfig(BaseModel):
    watchlist: list[WatchlistEntry]
    bar_timeframe: str
    lookback_bars: int
    audit_log_path: str
    risk: RiskThresholds


class Settings(BaseModel):
    alpaca_api_key: str
    alpaca_secret_key: str
    trading_mode: str
    log_level: str = "INFO"
    config: AppConfig

    @field_validator("trading_mode")
    @classmethod
    def _must_be_paper(cls, value: str) -> str:
        if value != "paper":
            raise ValueError(
                f"TRADING_MODE={value!r} is not supported in this slice. "
                "Live trading is structurally unreachable until the unlock "
                "criteria are met and code beyond this slice is added. "
                "Set TRADING_MODE=paper."
            )
        return value

    @classmethod
    def from_env(
        cls,
        env_path: str | Path | None = None,
        config_path: str | Path | None = None,
    ) -> "Settings":
        load_dotenv(dotenv_path=env_path)

        raw_config_path = Path(config_path) if config_path else Path("config.yaml")
        with open(raw_config_path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)

        app_config = AppConfig(
            watchlist=raw["watchlist"],
            bar_timeframe=raw["bar_timeframe"],
            lookback_bars=raw["lookback_bars"],
            audit_log_path=raw["audit_log_path"],
            risk=RiskThresholds(
                max_risk_per_trade_pct=raw["max_risk_per_trade_pct"],
                max_position_value_pct=raw["max_position_value_pct"],
                daily_loss_cap_pct=raw["daily_loss_cap_pct"],
                max_open_positions=raw["max_open_positions"],
                min_reward_risk_ratio=raw["min_reward_risk_ratio"],
                correlation_basket_max=raw["correlation_basket_max"],
                slippage_max_pct=raw["slippage_max_pct"],
                market_edge_buffer_minutes=raw["market_edge_buffer_minutes"],
                kill_switch_consecutive_losses=raw["kill_switch_consecutive_losses"],
                kill_switch_api_errors=raw["kill_switch_api_errors"],
                kill_switch_api_error_window_minutes=raw[
                    "kill_switch_api_error_window_minutes"
                ],
            ),
        )

        return cls(
            alpaca_api_key=os.environ.get("ALPACA_API_KEY", ""),
            alpaca_secret_key=os.environ.get("ALPACA_SECRET_KEY", ""),
            trading_mode=os.environ.get("TRADING_MODE", "paper"),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
            config=app_config,
        )
