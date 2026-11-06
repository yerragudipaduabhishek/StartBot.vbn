# Security and Risk — BTC AI Trading System

**Date:** 2026-10-28
**Scope:** current paper/testnet prototype and the planned evolution in `IMPLEMENTATION_PLAN.md`
**Current live-trading status:** disabled; **not authorized**
**Related:** [`PROJECT_AUDIT.md`](PROJECT_AUDIT.md), [`ARCHITECTURE.md`](ARCHITECTURE.md),
[`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md)

---

## 1. Scope and assumptions

This document covers:

- capital safety (primarily future live risk, and current paper correctness);
- invalid/stale data, bad signals, and risk bypass;
- accidental order placement or venue confusion;
- credential and API exposure;
- restart, reconciliation, and state corruption;
- AI/self-improvement misuse and overfitting;
- operational response.

Assumptions:

- The system continues to run in **paper mode** until a separate live-readiness review is approved.
- The host, Docker daemon, database volume, and `.env` are controlled by the operator.
- Exchange credentials, when present, are testnet credentials and are not committed.
- The current regression suite is the gate for all changes.

---

## 2. Non-negotiable safety invariants

1. **No real order** is placed unless a separate, signed live-readiness process is complete. This plan
   does not authorize it.
2. **Paper is the default.** Missing configuration must not select live.
3. **Risk is deterministic and human-owned.** AI cannot read-write risk limits, approve itself, or
   bypass a check.
4. **Fail closed.** Missing/stale/invalid data, absent risk approval, unknown state, or reconciliation
   mismatch blocks new entries.
5. **One order path.** Paper, testnet, and future live orders all pass through one risk-gated order
   manager.
6. **Idempotency.** Every order has a unique client order ID and a persisted state; restart cannot
   duplicate it.
7. **Kill switch.** A persisted halt must stop new entries and survive restart; resume is explicit and
   audited.
8. **No secrets in code, logs, docs, tests, or chat.** Use env/secret manager only.
9. **Every decision is attributable** to a strategy version, dataset, feature version, risk decision,
   and timestamp.
10. **No automatic promotion.** Strategy changes require validation, review, human approval, and a
    paper canary.

---

## 3. Threat model

| ID | Threat / vector | Impact | Current controls | Current gaps | Planned control |
|---|---|---|---|---|---|
| T1 | Accidental live order | Real capital loss | `LIVE_MODE` off, testnet on, confirmation flag, Executor flags | Direct `DeltaClient` has no host interlock; live path sends naked market order; no signed readiness | Single order manager, host interlock, live adapter disabled, signed checklist |
| T2 | Strategy/config mismatch | Trades an unvalidated strategy | README documents 1H Strategy A | Main runner defaults to 5m and default engine params; hard-coded DB labels | `TradingProfile`, startup refusal, profile persisted |
| T3 | Invalid/incomplete signal | Unintended entry | Signal engine normally sets fields | `PaperTrader` defaults `risk_allowed=True`; fabricates stop/target | Typed signal + fail-closed validator |
| T4 | Risk bypass | Oversized losses | Confidence/daily/drawdown/vol sizing exist | Closed P&L not recorded; peak/day not updated; Executor trusts caller size; no order caps | Persisted risk lifecycle, mandatory decision at order manager |
| T5 | Stale/bad/mixed-source data | Wrong trades | Market health, freshness snapshot, dedupe | No boundary gate; fixed 300s for all timeframes; silent production read fallback; no row provenance | Quality report, resolution-aware freshness, source labels, quarantine |
| T6 | Restart duplication/omission | Duplicate/missed trades, wrong risk | Paper state restores; testnet recovery | Main runner can auto-enter on first cycle; no runner state; no paper order ledger | Startup seed, idempotency key, reconciliation |
| T7 | API abuse / unauthorized mutation | Data/CPU/DB impact, watchlist tampering | CORS localhost | No auth, binds `0.0.0.0`, mutation endpoints, no rate limit | Auth, rate limit, localhost/proxy, admin-only mutations |
| T8 | Credential exposure | Account compromise | `.env` ignored, no tracked secrets, health avoids secrets | No secret manager/scanning; local `tradingBot/.env` exists outside repo; keys may be broad | Secret manager, least-privilege/withdrawal-disabled keys, rotation, scanning |
| T9 | Supply-chain/dependency drift | Compromise/breakage | Node lockfile | Python `>=` unpinned, no Python lock/scan; base image not digest-pinned | Lock/pin, CI audit, image digest, minimal deps |
| T10 | Database tampering/corruption | False history, bad risk state | SQLite file volume | No audit table, no WAL/timeout, no integrity checks, no backup drill | Append-only audit, WAL, backups, restore tests |
| T11 | Overfit/leaky research | False confidence, losses | OOS study exists | Walk-forward does not re-fit; no artifacts; hard-coded "VALIDATED" | Rolling walk-forward, fold registry, artifacts, reviewer veto |
| T12 | AI prompt injection | Harmful proposal/patch | No AI implemented | Future untrusted news/LLM text | Treat external text as data, schema validation, sandbox, reviewer, human approval |
| T13 | AI/self-improvement overreach | Risk/execution changes | None (not implemented) | Future import/DB access risk | Import boundary, separate process/DB role, no auto-apply, approvals |
| T14 | Insider/human error | Unsafe config/promotion | Flags, tests | No four-eyes/audit for limit changes | Versioned limits, approval records, audit trail |
| T15 | DoS / resource exhaustion | Missed management/alerts | Basic health | No rate limit, no resource limits, expensive polling | Rate limits, timeouts, circuit breaker, container limits, alerts |
| T16 | Silent alert/monitor failure | Undetected incident | Health endpoints | No alert delivery, no heartbeat alert | Alert adapter, dead-man/heartbeat alert, dry-run tests |

---

## 4. Verified current controls

These are genuinely present and should be preserved.

### 4.1 Execution safety

- Defaults: `live_mode=False`, `testnet_mode=True`, `real_order_confirmation=False`
  (`app/config/settings.py:59-61`).
- `Executor.can_trade_live` requires live mode, testnet off, confirmation, and keys
  (`app/trading/executor.py:57-70`); `execute()` also requires `confirm_real_orders=True`.
- `TestnetExecutionClient` validates the venue host and refuses to initialize with live mode on or
  testnet mode off (`app/execution/client.py:61-97`); production order hosts are hard-blocked.
- Testnet orders carry client order IDs and duplicate protection; recovery/monitor/report code
  persists state and reconciles on reconnect (`app/execution/recovery.py`, `feed.py`, `report.py`).
- The paper path does not call a Delta order endpoint and requires no API keys.
- Under default configuration the reviewed code has no reachable real-order path. No real-money
  order was exercised during this audit; testnet execution records were not independently replayed.

### 4.2 Secrets and data

- No `.env` or tracked secret-like files were found in the repository during the audit.
- `.env` is ignored by `.gitignore`; `.env.example` is empty.
- `/health` exposes operational status, not keys or signatures.
- Testnet journals/logs are designed for execution evidence, not credentials.
- The dashboard labels paper results as simulated.

### 4.3 Testing

- The full test suite passes without credentials or real network orders.
- Safety tests cover live/testnet gates, duplicate protection, recovery, paper persistence, risk
  basics, websocket data handling, and dashboard contracts.
- Test isolation uses temporary SQLite storage.

### 4.4 Known local hygiene item

`C:\Users\abhia\tradingBot` (outside this repository) contains a local `.env` with ≥1 secret-like
assignment. It was not read into logs or modified. The owner should confirm whether it is still needed,
restrict permissions, and rotate any credential that may have been exposed to other tools.

---

## 5. Open security/risk gaps

Severity follows the audit: **P1 = before any live consideration**, **P2 = before scaling**.

| ID | Severity | Gap | Consequence |
|---|---|---|---|
| G1 | P1 | Paper accepts missing `risk_allowed` as true and fabricates risk levels | Unapproved/incomplete signal can open a trade |
| G2 | P1 | Risk state is not updated on paper close and is not restored after restart | Daily-loss/drawdown gates ineffective; wrong sizing; halt bypass |
| G3 | P1 | Risk is not enforced at the execution boundary; Executor trusts size | Any caller can place oversized orders; future live path unsafe |
| G4 | P1 | Main paper runner defaults to 5m/default engine, not Strategy A 1H | Operator may believe the validated strategy is running |
| G5 | P1 | Main runner can auto-enter on the first cycle after restart | Unintended entry contrary to README |
| G6 | P1 | No order caps, cooldown, daily trade cap, exposure caps, spread/slippage gates, sessions, duplicate guard | Risk limits ignored in practice |
| G7 | P1 | No kill switch and no alerting | Incidents can continue undetected; stop requires process kill |
| G8 | P1 | API is unauthenticated and bound to `0.0.0.0`; mutation endpoints | Remote tampering/DoS; watchlist changes |
| G9 | P1 | No data-quality/provenance gate at the trade boundary; production read fallback | Stale/mixed-source data can drive paper decisions |
| G10 | P2 | Direct `DeltaClient` has no production-host interlock | Defense-in-depth gap outside Executor |
| G11 | P2 | No paper order/fill/risk-decision/equity audit trail | Trades cannot be fully reconstructed |
| G12 | P2 | Ad-hoc migrations, no audit/experiment tables, no WAL/backups | Data loss/migration failure; no incident forensics |
| G13 | P2 | Unpinned Python dependencies; no lint/type/security scan | Supply-chain and regression risk |
| G14 | P2 | Backtest metric/accounting errors and non-reproducible "validated" claim | Capital allocation based on overstated evidence |
| G15 | P2 | No auth/rate limiting for analysis endpoints | Cost/rate-limit exhaustion |
| G16 | P3 | Test collection warnings/line-ending noise | Maintenance and signal-to-noise risk |
| G17 | P3 | No structured logging/alert delivery | Weak detection/response |

### 5.1 Failure modes worth spelling out

- **Kill-switch absence:** today, stopping the container prevents new paper entries but does not
  manage an open position; a testnet open order/position may remain. A real kill switch must halt
  entries, optionally flatten, and report state.
- **Restart resets:** restarting to "clear" a drawdown/loss halt is currently possible because risk
  state is in memory. That is a control failure, not a recovery strategy.
- **Public-data fallback:** production public candles are allowed while testnet mode is on. This is
  funds-safe but can contaminate paper/backtest provenance; rows must be labeled and treated
  consistently.
- **Direct client use:** the testnet guard lives in `TestnetExecutionClient`, not in the transport.
  Future code must not construct a production `DeltaClient` for orders.
- **Paper ≠ live:** paper fills at candle close with modeled slippage; this is not evidence of live
  fills, latency, funding, or liquidation behavior.

---

## 6. Hard development rules

1. **Do not enable live trading** in this phase. Do not remove/weaken the flags or the testnet host
   guard.
2. **No real orders in tests, CI, demos, or audits.** Use fakes/mocks; the testnet round-trip is a
   separate, explicit, credentialed activity.
3. **Do not let AI touch risk or execution.** Research code must not import `app.risk` limits,
   `OrderManager`, or any broker. Enforce with an import-boundary test.
4. **Do not accept caller-supplied risk approval.** Only the deterministic risk engine can produce a
   `RiskDecision`.
5. **Do not add permissive defaults.** Missing/invalid state rejects, never permits.
6. **Do not log secrets or full request headers.** Redact keys, signatures, and tokens.
7. **Do not silently fall back** to a different venue, dataset, or model without labeling it in the
   decision/result.
8. **Do not promote a strategy or change a risk limit without a recorded human approval.**
9. **Do not merge risk/execution changes without tests** covering the failure path.
10. **Do not share paper/testnet/live databases or state directories.** Use separate profiles.
11. **Do not trust external text.** News, calendars, and LLM outputs are data, not instructions.
12. **Preserve the audit trail.** Orders, fills, decisions, approvals, and config changes are
    append-only.

---

## 7. Secrets and credential policy

### Storage

- Development: `.env` only, ignored by git, file permissions restricted to the operator.
- Production: environment injection from a secret manager or Docker secrets; no `.env` volume if
  avoidable.
- Separate credentials for testnet and live; live credentials must never be present on the paper host.
- Exchange keys: trade-only, withdrawal disabled, IP allowlisted where supported, minimum permissions.
- Dashboard/API: separate read-only and admin tokens; admin token unset disables admin endpoints.

### Logging and errors

- Never log API keys, secrets, signatures, auth headers, or full exchange payloads containing account
  data.
- Redact query strings that may contain signatures.
- Client-facing errors return codes, not internal stack traces.

### Rotation and exposure response

1. Revoke/rotate the credential at the exchange immediately.
2. Halt trading (kill switch or stop runner); cancel open testnet orders; assess positions.
3. Review exchange order/withdrawal history and application logs.
4. Preserve evidence; do not rewrite logs.
5. Patch the exposure path; add a regression test.
6. Record an incident entry with timeline and actions.

### Planned

- Pre-commit/CI secret scanning; dependency and container scanning; no secrets in build args;
  encrypted backups; periodic rotation drill.

---

## 8. API and deployment security

### Current exposure

- FastAPI binds `0.0.0.0:8000`; Docker publishes it.
- No authentication; CORS is `http://localhost:3000` but CORS is not auth.
- Watchlist add/remove/select are unauthenticated mutations.
- No rate limiting; `/analysis/{symbol}` can trigger repeated external calls and indicator work.

### Target

- Bind `127.0.0.1` by default; expose only through a TLS reverse proxy with auth.
- Read endpoints can use a read-only token; mutations/admin/kill switch require a separate admin
  token and audit log.
- Rate limit by IP/token; cap request sizes; timeouts on all external calls.
- Restrict CORS per environment; validate/allowlist symbols; no arbitrary URLs.
- Container: non-root user, read-only root filesystem where practical, resource limits, healthcheck.
- Firewall: only proxy ports exposed; database volume not network-shared.
- Backups encrypted and access-controlled; restore tested.

---

## 9. Operational runbooks

### R1 — Stop new trading (current workaround, until kill switch exists)

1. Stop the app/runner container or process.
2. Confirm no new signals/orders are being created.
3. Note that an open **paper** position is no longer monitored; it will not auto-close.
4. For testnet, manually cancel open orders and reconcile/flatten positions per the exchange UI/API.
5. Preserve logs, DB, and telemetry; record the action.

**Target:** `python -m app.risk.kill_switch --halt --reason "..."`, verify persisted status, alert,
and optionally flatten via the order manager with explicit human confirmation.

### R2 — Daily-loss or drawdown breach

1. Verify the risk state and equity snapshot; do not restart to reset counters.
2. Halt new entries; keep or flatten positions per the approved policy.
3. Reconcile paper/testnet state.
4. Identify cause (data, strategy, bug, costs) before resuming.
5. Resume only with human approval; record rationale.

### R3 — Stale, invalid, or mixed-source data

1. Check `/health`, `data_status`, source, last candle age, and feed errors.
2. Halt new entries; do not manually override the freshness gate.
3. Switch to the intended venue/source; verify provenance labels.
4. Backfill/validate; record the incident.
5. Resume only when quality is `ok` and the source is intended.

### R4 — Order rejection loop / broker outage

1. Stop retrying; inspect order manager/testnet journal and venue status.
2. Reconcile positions/orders; never assume a rejected order did not fill.
3. Cancel orphans; confirm flat/exposure.
4. Fix the cause; add a regression test.
5. Record incident and exposure.

### R5 — Reconciliation mismatch

1. Halt new entries.
2. Snapshot DB, logs, and venue positions/orders.
3. Compare orders, fills, positions, balances, and equity.
4. Do not auto-correct silently; resolve manually with a recorded adjustment/event.
5. Add a test reproducing the mismatch before resuming.

### R6 — Suspected credential compromise

Follow §7 rotation, plus: isolate the host, review processes/network, rotate all credentials, restore
from a known-good state if needed, and review all orders/fills. Treat any real order as a SEV1.

### R7 — Database corruption or failed migration

1. Halt the app; preserve the corrupt file.
2. Restore the latest verified backup to a separate path.
3. Run integrity checks and reconciliation.
4. Verify strategy/risk/experiment rows; replay artifacts if possible.
5. Document data loss (if any) and notify affected decisions.

### R8 — Live enablement (not authorized)

A live decision requires, at minimum:

- all P1 gaps closed and independently reviewed;
- single order manager + host interlock + native exchange stops;
- persisted risk lifecycle and kill switch drilled;
- reconciliation, alerts, and incident runbooks tested;
- authenticated API, secret management, and least-privilege keys;
- reproducible validation artifacts and a paper canary;
- a signed readiness record naming the approver, limits, and rollback plan;
- separate deployment/DB/credentials and a small-capital phase.

Until all are true, live remains disabled. No checklist item may be waived by an AI agent.

---

## 10. Incident response and audit

### Severity guide

- **SEV1:** real order/capital loss, credential compromise, or live guard bypass.
- **SEV2:** incorrect paper risk/accounting, data integrity failure, reconciliation mismatch, kill
  switch failure.
- **SEV3:** dashboard/API/alert outage with no trading impact, non-critical bug.

### Immediate actions

1. Halt new trading and preserve evidence.
2. Reconcile positions/orders and quantify exposure.
3. Contain (rotate secrets, isolate host, disable endpoint).
4. Recover from known-good state.
5. Record timeline, root cause, and corrective actions.
6. Add regression tests and update runbooks.

### Audit trail requirements (target)

- Append-only `audit_events` for config/limit changes, kill switch, approvals, promotions, rollbacks,
  and manual reconciliations.
- Immutable `orders`, `fills`, and `risk_decisions` with correlation IDs.
- Strategy/feature/dataset versions on every signal and experiment.
- Alert delivery attempts and outcomes, with dedupe.
- Actor identity and rationale for every human action.
- Retention policy and backup/restore verification.

Current state: testnet journals/telemetry exist, but there is no unified audit table and no
tamper-evidence. This is a P2 gap.

---

## 11. Security testing requirements

Before merging safety/execution changes:

- Fail-closed tests for every validation/risk rule.
- Live/testnet guard tests, including direct-client host interlock.
- Restart/idempotency/reconciliation tests.
- Kill-switch tests, including persistence and resume authorization.
- Alert sink tests (no real alerts in tests).
- Auth/rate-limit/CORS tests.
- Secret-redaction tests for logs/errors.
- Import-boundary test proving research cannot import execution/risk/order modules.
- Dependency/container scan in CI.
- Offline-only test suite: no network, no credentials, no real orders.

Coverage alone is not evidence; the tests must assert the specific failure modes.

---

## 12. Residual risk and sign-off

Even after all planned controls:

- Markets can lose money; no strategy is guaranteed.
- Paper results overstate live performance; live fills, funding, latency, and liquidation differ.
- Software can fail in unanticipated ways; defense-in-depth and small size limits remain necessary.
- AI proposals can be plausible but wrong; human review and validation gates are required.
- SQLite single-node deployment has availability limits.

**Current sign-off:** paper/testnet development may continue under this plan. Live trading is
**not approved**. The open P1 gaps in §5 must be closed and reviewed before any live-readiness
discussion.
