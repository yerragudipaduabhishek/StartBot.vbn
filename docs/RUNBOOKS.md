# Runbooks — Paper-First Operations

All procedures below operate on **paper/testnet only**. `LIVE_MODE` and
`REAL_ORDER_CONFIRMATION` must remain `false`; `ENVIRONMENT=staging|live`
refuses to boot when either is true. No runbook authorizes real orders.

## 0. Quick reference

```powershell
# Backend (local)
.\.venv\Scripts\python.exe -m app

# Full test suite
.\.venv\Scripts\python.exe -m pytest -q

# Lint
.\.venv\Scripts\python.exe -m ruff check app tests

# Docker (backend + dashboard)
docker compose up --build -d
docker compose ps
docker compose logs -f app

# Health
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/monitor

# Backup both databases (consistent snapshots + integrity check)
.\.venv\Scripts\python.exe -m app.ops.backup `
  --source data/trading.db --source data/research.db --dest backups/
```

## 1. Start / stop

1. Confirm `.env` has `LIVE_MODE=false`, `REAL_ORDER_CONFIRMATION=false`,
   `TESTNET_MODE=true`.
2. Start with `python -m app` or compose. On Linux, create writable volume
   directories first: `mkdir -p data logs && sudo chown -R 10001:10001 data logs`
   (the backend image runs unprivileged as uid 10001).
3. Verify `GET /health` returns `status=ok`; `ready=true` only when market data
   is streaming. A degraded feed is not a reason to bypass risk checks.
4. Stop with Ctrl+C (clean shutdown) or `docker compose stop app`. The paper
   account and kill switch state persist in SQLite.

## 2. Emergency halt

Preferred order: risk engine halt -> bot halt -> kill switch -> stop process.

```powershell
# Dashboard: "Emergency halt" with admin token; or:
curl -X POST http://127.0.0.1:8000/bot/emergency-halt `
  -H "Content-Type: application/json" -H "X-Admin-Token: $env:ADMIN_TOKEN" `
  -d '{"reason":"operator emergency halt"}'

# Persistent kill switch (survives restart)
.\.venv\Scripts\python.exe -m app.risk.kill_switch --halt --reason "operator halt"
```

After halting, verify `GET /kill-switch` shows halted and `GET /bot/status`
reports `risk_halted=true`. Investigate before resuming; resume requires an
explicit admin token / confirm flag. Never edit SQLite rows directly to clear a
halt.

## 3. Risk halt / drawdown breach

1. `GET /risk` and `GET /risk-decisions?limit=20` show the blocking reason.
2. `GET /equity` and `GET /metrics` confirm equity/drawdown history.
3. Do not raise limits in the same session. Record the incident, reconcile
   paper state (`GET /paper/account`, `GET /paper/position`), and only then
   resume through the normal admin endpoint.
4. If reconciliation fails on restart, the app logs `paper/risk reconciliation
   failed` and halts. Stop and restore the last verified backup (section 7).

## 4. Stale or missing market data

1. `GET /health` (`data_status`, `last_update_unix`) and `GET /monitor`
   identify stale symbols/feeds.
2. The freshness gate blocks new entries; open paper positions remain managed.
3. Check network/DNS and Delta public hosts. Fallback public-data reads are
   GET-only and cannot place orders.
4. Do not disable the freshness gate. If the outage persists, halt and wait.

## 5. Testnet / broker outage

1. `python -m app.execution sweep` reports auth/product/position status.
2. Execution guard fails closed on missing data, reconciliation mismatch, or
   broker errors. Repeated `409/5xx` from the venue must not be retried
   blindly; the validator/loop records the failure.
3. Paper trading continues; no real order path is reachable.
4. If credentials are suspected compromised: rotate keys (section 9), restart,
   and review `logs/testnet_executions.jsonl`.

## 6. Stale or blocked AI proposal

1. Open `/research`; inspect gate truth for the proposal.
2. Failed gates cannot be overridden. Fix the artifact/dataset/metrics and
   create a new proposal.
3. Reviews are append-only. A veto blocks promotion; a fresh proposal is
   required after addressing findings.

## 7. Backup and restore drill

Run this drill monthly and before any upgrade.

```powershell
# 1. Consistent backup of both databases
.\.venv\Scripts\python.exe -m app.ops.backup `
  --source data/trading.db --source data/research.db --dest backups/

# 2. Verify integrity (the CLI already checks; manual check shown)
.\.venv\Scripts\python.exe -c "from app.ops.backup import verify_database; print(verify_database('backups/trading-<stamp>.db'))"

# 3. Stop the backend before a restore.
# 4. Restore trading DB (keeps target.pre-restore automatically)
.\.venv\Scripts\python.exe -m app.ops.backup `
  --restore backups/trading-<stamp>.db --target data/trading.db --overwrite

# 5. Restore research DB the same way, then start the backend and check /health.
```

Rules:

- Never copy a live SQLite file with Explorer/`copy`; use the backup API.
- Keep at least 7 daily and 4 weekly backups off the host.
- A restore without a verified integrity check is not complete.
- If the restore fails, the pre-restore copy remains at
  `data/trading.db.pre-restore*`.

## 8. Logs, disk, and rotation

- `LOG_FILE` enables JSON logs with `LOG_MAX_BYTES` (default 10 MB) and
  `LOG_BACKUP_COUNT` (default 5) rotating files.
- Check disk monthly: `Get-PSDrive C`. Log growth or `database or disk is full`
  alerts require freeing space or moving `data/`, `logs/` to a larger volume.
- Do not delete the active SQLite file or its `-wal`/`-shm` siblings.

## 9. Credential rotation

1. Rotate `ADMIN_TOKEN` in `.env`/secret store; restart; confirm old token gets
   `403`.
2. Rotate Delta testnet keys; restart; run `python -m app.execution sweep`.
3. Rotate LLM/news provider keys; agents remain read-only and budget-limited.
4. Never log or commit tokens; `git grep`-style secret scans run in CI.

## 10. Strategy promotion and rollback

1. Promotion requires experiments, artifacts, gates, review and approval stored
   in the research DB.
2. `POST /research/proposals/{id}/promote` only succeeds when gates pass; the
   previous version is recorded.
3. Roll back with `POST /research/strategies/{strategy_id}/rollback` and a
   reason. Verify `/research` shows the rollback and active version.
4. Rollback never edits risk limits or enables live execution.

## 11. Soak test

```powershell
.\.venv\Scripts\python.exe -m app.soak --duration 3600 --out data/soak_telemetry.jsonl
# or: docker compose --profile soak up -d soak
```

Review telemetry for feed gaps, risk halts, reconciliation failures and
drawdown. The soak banner must read PAPER TRADING / NOT REAL FUNDS / LIVE OFF.

## 12. Incident record

For every halt/outage, record: UTC start/end, symptom, affected component,
safety state (kill switch, live flag, open paper position), evidence
(`/audit`, `/alerts`, logs), action taken, restore/backup used, and follow-up
owner. A postmortem is required for any risk halt longer than 15 minutes.
