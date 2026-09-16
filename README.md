# trading-bot

A hexagonal-architecture paper-trading bot: a technical support/resistance
breakout signal, a 9-guardrail risk manager, and an in-process hourly
orchestrator, running against Alpaca's **paper trading API only**.

This is the first delivery slice. Live trading is intentionally **not
implemented** — see [Live-mode unlock criteria](#live-mode-unlock-criteria)
and the [disclaimer](#disclaimer) below.

## Architecture

```
src/trading_bot/
  domain/        # frozen dataclasses: Bar, Signal, TradeIntent, RiskDecision, ...
  ports/         # BrokerPort, SignalPort (typing.Protocol interfaces)
  adapters/      # AlpacaBrokerAdapter (paper mode only)
  signals/       # TechnicalSignal (S/R swing-pivot breakout detector)
  risk/          # 9 pure Guardrail classes + RiskManager + SessionState
  orchestrator.py
  audit.py       # JSONL decision log
  config.py      # Settings.from_env()
  __main__.py    # composition root
```

The `Orchestrator` is constructed via dependency injection only
(`BrokerPort`, `SignalPort`, `RiskManager`) — it never imports a concrete
adapter directly, so it can be driven end-to-end in tests with mocks.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
pip install -e .
```

Copy `.env.example` to `.env` and fill in your **paper trading** credentials
from the [Alpaca paper dashboard](https://app.alpaca.markets/paper/dashboard/overview):

```
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
TRADING_MODE=paper
LOG_LEVEL=INFO
```

`config.yaml` holds non-secret configuration: the watchlist
(AAPL, MSFT, SPY, NVDA, GOOGL), bar timeframe (1H), and all 9 risk
guardrail thresholds. Anything a reviewer should see in a diff belongs
there; anything that can leak money (API keys) belongs in `.env`.

## Running in paper mode

```bash
python -m trading_bot
```

This starts an in-process loop that sleeps until the next hour boundary
(+30 seconds), then runs one cycle: fetch bars for the watchlist, generate a
signal per symbol, validate any non-HOLD signal through all 9 guardrails,
place the order if approved, and append one JSONL record per symbol to
`logs/audit.jsonl` regardless of outcome (HOLD, REJECTED, EXECUTED, ERROR,
SKIPPED, or HALTED).

`Settings.from_env()` raises immediately if `TRADING_MODE` is anything other
than `"paper"` — this is a hard startup gate, not a runtime branch.

## The 10 guardrails

`RiskManager.evaluate()` runs all 10 guardrails and does **not** short-circuit
so every verdict is recorded in the audit log — except the kill switch,
which short-circuits to an immediate rejection when active.

1. **Max risk per trade** — reject if risked capital > 1% of equity.
2. **Position sizing cap** — reject if resulting position value > 10% of equity.
3. **Daily loss cap** — reject all new trades once daily P&L reaches -3%.
4. **Max open positions** — reject if it would exceed 5 concurrent positions.
5. **Min reward/risk ratio** — reject if reward:risk < 2.0.
6. **Correlation cap** — reject a 3rd concurrent position in the same
   correlated basket (tech: AAPL/MSFT/NVDA/GOOGL, max 2; SPY is its own
   `index` basket).
7. **Market hours window** — reject outside regular hours or within 5
   minutes of open/close.
8. **Slippage guard** — reject if the latest quote drifted > 0.3% from the
   signal's reference price.
9. **Kill switch / circuit breaker** — halts all new trades on 3 consecutive
   losses, more than 5 API errors in a rolling 15-minute window, or the
   daily loss cap being hit. The halt **requires an explicit manual reset**
   (`SessionState.manual_reset()`) — it does not auto-clear on next cycle,
   new trading day, or process restart.
10. **No duplicate position** — reject any new entry on a symbol that
    already has an open position. Added after a production incident: with
    no memory across cycles, the orchestrator otherwise re-enters the same
    symbol on every cycle a breakout signal still holds, stacking
    uncoordinated risk instead of letting the existing position's bracket
    order (stop-loss/take-profit) close it out on its own.

## Testing

```bash
pytest
```

Smoke coverage: one pass/fail test per guardrail, a `RiskManager`
aggregation test (multiple simultaneous failures) and kill-switch
short-circuit test, technical-signal breakout/hold fixtures, an
end-to-end orchestrator cycle test with a mocked `BrokerPort` (no network
calls), rejected-trade no-op, halted-cycle no-op, per-symbol
`BrokerError` isolation, and a live-mode-gate rejection test.

## Live-mode unlock criteria

Live trading is **structurally unreachable** in this slice — there is no
code path that places a real order. Before live trading code is even
written, ALL of the following must be true, evaluated over paper trading
history:

- At least **30 consecutive paper trading days**.
- At least **50 executed trades**.
- Win rate **>= 50%**.
- Max drawdown **<= 10%**.
- Profit factor **>= 1.3**.
- **Zero kill-switch trips** in the final 10 trading days.
- Explicit **human sign-off** recorded.

## Deployment (EC2 / any always-on Linux host)

The bot must run continuously to accumulate a meaningful paper-trading
track record — closing your laptop kills the loop and resets the clock on
the [live-mode unlock criteria](#live-mode-unlock-criteria). Deploy it to a
small always-on instance instead (e.g. an EC2 `t3.micro`).

```bash
# On the instance — clone wherever you like, no need to move it to /opt:
git clone <your-repo-url>
cd trading-view

# Copy your .env (paper credentials) over — never via git, never in shell
# history. scp it, or paste it interactively with `cat > .env` then chmod 600.

sudo bash deploy/setup_ec2.sh
sudo systemctl start trading-bot
sudo journalctl -u trading-bot -f   # tail logs
```

`setup_ec2.sh` detects the repo path automatically and generates the
systemd unit for wherever you cloned it (`/opt/...`, `/home/ubuntu/...`,
anywhere).

`deploy/trading-bot.service` runs the bot as a dedicated, unprivileged
`trading-bot` system user (no login shell) under systemd, restarts it
automatically on crash (capped at 5 restarts / 10 minutes to avoid a
crash-loop against Alpaca's API), and confines its filesystem writes to
`logs/`. See `deploy/setup_ec2.sh` for the full bootstrap (Python
3.12 + venv + service user + systemd unit).

## Disclaimer

**This slice does not implement live trading.** Enabling live trading
requires writing new code beyond this scope (a live order-placement path,
additional safeguards, and satisfying every criterion above) plus explicit
human review and sign-off. Setting `TRADING_MODE=live` today raises an
error at startup — it is not a configuration flag that unlocks real money
trading.
