"""Composition root: build Settings, Alpaca clients, adapters, and the
Orchestrator, then run the loop. This is the ONLY module that constructs
concrete adapters and reads the environment.
"""

from __future__ import annotations

import logging

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.trading.client import TradingClient

from trading_bot.adapters.alpaca_broker import AlpacaBrokerAdapter
from trading_bot.audit import AuditLogger
from trading_bot.config import Settings
from trading_bot.orchestrator import Orchestrator
from trading_bot.risk.manager import RiskManager
from trading_bot.signals.technical import TechnicalSignal


def build_orchestrator(settings: Settings) -> Orchestrator:
    trading_client = TradingClient(
        settings.alpaca_api_key, settings.alpaca_secret_key, paper=True
    )
    data_client = StockHistoricalDataClient(
        settings.alpaca_api_key, settings.alpaca_secret_key
    )
    broker = AlpacaBrokerAdapter(trading_client, data_client)
    strategy_cfg = settings.config.strategy
    signal_source = TechnicalSignal(
        trend_ma_period=strategy_cfg.trend_ma_period,
        volume_lookback=strategy_cfg.volume_lookback,
        volume_confirmation_multiplier=strategy_cfg.volume_confirmation_multiplier,
        min_level_touches=strategy_cfg.min_level_touches,
        level_touch_tolerance_pct=strategy_cfg.level_touch_tolerance_pct,
    )
    risk_manager = RiskManager(settings.config.risk)
    audit_logger = AuditLogger(settings.config.audit_log_path)
    watchlist = [entry.symbol for entry in settings.config.watchlist]

    return Orchestrator(
        broker=broker,
        signal_source=signal_source,
        risk_manager=risk_manager,
        audit_logger=audit_logger,
        watchlist=watchlist,
        lookback_bars=settings.config.lookback_bars,
        daily_loss_cap_pct=settings.config.risk.daily_loss_cap_pct,
    )


def main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(level=settings.log_level)
    orchestrator = build_orchestrator(settings)
    orchestrator.run_forever()


if __name__ == "__main__":
    main()
