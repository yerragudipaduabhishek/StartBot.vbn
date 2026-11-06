# Security and Safety

## Release posture

This release is paper/research focused. `LIVE_TRADING_ENABLED` is hard-coded false. Startup validation rejects `LIVE_MODE=true`, `REAL_ORDER_CONFIRMATION=true`, and unsafe testnet configuration. TradingProfile rejects live mode. The live broker cannot be constructed.

## Execution safeguards

- Paper execution is local and does not call Delta order endpoints.
- Testnet execution requires testnet mode and is guarded against production hosts.
- Research/backtest modes cannot submit orders or persist trading state.
- ExecutionGuard validates the signal/profile, kill switch, risk decision, portfolio limits and exact accepted size.
- Adapters revalidate mode, risk approval, quantity, signal key, price, stop, target and strategy identity.
- Duplicate signal/order protections and persisted execution intents support idempotency and recovery.

## API safeguards

The backend defaults to loopback binding. Staging/live profile startup requires an admin token, explicit CORS allowlist and safe bind configuration. The middleware adds security headers, enforces request body limits and an in-process per-client rate limit; health and metrics probes are exempt. Admin control and research mutation endpoints require constant-time comparison of `X-Admin-Token`.

CORS is not authentication. A public deployment should use a TLS reverse proxy, firewall, strong admin token, strict origin allowlist and no direct public backend exposure.

## Secrets

Keep `.env` local and untracked. Commit only placeholder examples. Use separate testnet credentials and least-privilege keys; never enable withdrawals. Prefer secret injection/secret manager over baking credentials into images. Do not log keys, secrets, signatures or auth headers. Rotate credentials immediately if exposure is suspected.

## Data integrity and research boundaries

The shared feature pipeline rejects malformed, duplicate, out-of-order and invalid OHLCV candles. Dataset hashes and provenance are retained. Research agents treat external text as untrusted data, validate outputs against strict schemas and cannot self-apply code. Human review and approval are required for governance actions.

## Operational safety

- Keep `LIVE_MODE=false`, `REAL_ORDER_CONFIRMATION=false`, `TESTNET_MODE=true`.
- Use paper mode for routine operation.
- Treat stale/invalid/mixed-source data as a reason to stop new entries.
- Do not restart to clear a risk halt.
- Preserve logs, database files and execution journals during incidents.
- Verify backups by restoring to a separate location.
- Do not share paper, testnet and research databases/state directories.

## Readiness limitations

The repository's live-readiness review says NOT APPROVED. It records that the 30-day/100-trade continuous field soak has not been run, no named human approval is on file, no recent credentialed testnet round-trip is recorded, and external penetration testing/on-call rehearsal/backup restore evidence remain required. Follow the current readiness review rather than treating code presence as proof of operational validation.
