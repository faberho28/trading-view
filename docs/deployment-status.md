# Deployment Status

Snapshot as of 2026-09-09, mirrored from Engram (`trading-view` project,
topic key `sdd/real-trading-bot/deployment-status`). Engram is the source
of truth for SDD artifacts (proposal/spec/design/tasks/verify/archive);
this file exists so deployment history is also visible in git.

## Current state

The `real-trading-bot` first slice (paper mode) is deployed and running as
a systemd service on a cloud instance (Oracle Cloud, ARM64, Ubuntu 22.04),
under user `ubuntu`, checked out at `~/trading-view`. Service unit:
`trading-bot.service`, enabled and active as of 2026-09-09 ~01:00 UTC.

Goal: accumulate the 30-day / 50-trade paper-trading track record required
by the [live-mode unlock criteria](../README.md#live-mode-unlock-criteria).
Running locally was ruled out because the development machine shuts down.

## Deployment issues found and fixed (live, on a real instance)

- `deploy/setup_ec2.sh` originally hardcoded `/opt/trading-bot`; now
  auto-detects `APP_DIR` from `pwd`, since the repo was cloned to
  `~/trading-view` instead.
- Ubuntu 22.04 (jammy) doesn't ship `python3.12` by default (project
  requires `>=3.12`) — the script now falls back to the deadsnakes PPA.
- Running the service as a dedicated system user failed with
  `status=200/CHDIR`: that user couldn't traverse another user's home
  directory (mode 750), even with `ProtectHome` disabled. Fixed by running
  the service as the actual owner of the repo directory (`stat -c '%U'`)
  when the repo lives under `/home/*`, instead of creating a separate
  `trading-bot` system account.
- `StartLimitIntervalSec` / `StartLimitBurst` were originally under
  `[Service]` in the systemd unit — systemd silently ignores them there.
  Moved to `[Unit]`, where they take effect.

## Known non-bug behavior

The orchestrator wakes every UTC hour via
`_sleep_until_next_hour_boundary()` regardless of market hours, then asks
Alpaca's real clock (`BrokerPort.is_market_open()`) whether NYSE is
actually open. If not, guardrail #7 skips with
`SKIPPED, reason: market_hours`. This is correct but not optimized — it
wakes hourly even overnight instead of sleeping until the next market
open. Flagged as a possible future improvement, not a defect.

## Next check

Review `logs/audit.jsonl` and `sudo journalctl -u trading-bot` on the
instance after NYSE market open (2026-09-09, ~13:30 UTC / 9:30am ET) for
the bot's first real trading-hours cycle. Look for unexpected errors —
Alpaca API failures, malformed OHLCV bar shapes, or guardrail behavior
that doesn't match expectations — as opposed to expected `HOLD` or
`SKIPPED` outcomes.
