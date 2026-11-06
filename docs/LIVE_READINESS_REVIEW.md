# Live-Readiness Review (Separate Gate)

**Decision: NOT APPROVED for live trading.**

This release is a paper-trading and research platform. Live execution remains
unreachable by construction:

- `LIVE_MODE=true` fails startup validation.
- `REAL_ORDER_CONFIRMATION=true` fails startup validation.
- `TESTNET_MODE=false` is reported as an unsafe startup problem.
- `DeltaLiveBroker` refuses to construct.
- The dashboard and all runbooks state paper-only; no UI action enables live.

Passing this review would not automatically enable live trading. A future
change must be a separate, reviewed pull request that adds a signed live
profile, a second human approval, and venue-specific kill/reconciliation tests.

## Prerequisites for a future live review

| Area | Required evidence | Status |
|---|---|---|
| Paper soak | ≥ 30 days continuous, ≥ 100 trades, no unresolved reconciliation failure | Not run |
| Strategy validation | Walk-forward artifacts with OOS folds, min trades, drawdown bound, reviewed by a human | Artifacts produced per run; no 30-day field soak |
| Risk limits | Documented limits, daily loss stop, max exposure, leverage cap, tested halts | Paper tests exist; live limits unverified |
| Execution | Testnet round-trip, restart recovery, duplicate prevention, latency/fees measured | Testnet validator exists; no recent credentialed round-trip recorded |
| Data | Provenance, freshness, gap handling, fallback GET-only, outage drill | Implemented and tested |
| Security | Admin auth, rate limiting, body cap, CORS allowlist, secret rotation, dependency audit, TLS proxy | Implemented; external penetration test not performed |
| Backups | Restore drill with verified integrity, off-host retention | Procedure documented; drill evidence required |
| Observability | Metrics/alerts/audit trail reviewed during soak, on-call runbook | Implemented; on-call rehearsal not performed |
| Human approval | Named approver, signed rationale, rollback plan | No approval on file |
| Legal/venue | Delta production API terms, account permissions, IP allowlist | Not reviewed |

## Evidence checklist (attach to future request)

- [ ] 30-day soak telemetry summary and incident list.
- [ ] Walk-forward manifest hashes + reviewer sign-off.
- [ ] Testnet execution journal with entry/exit/SL/TP and reconciliation.
- [ ] Restore-drill output from `app.ops.backup` on a host copy.
- [ ] Dependency audit and secret-scan results.
- [ ] Risk-limit table and daily-loss-stop test evidence.
- [ ] Reverse-proxy TLS configuration and CORS allowlist diff.
- [ ] Rollback plan exercised on paper.
- [ ] Two named human approvers.

## Sign-off (future use only)

| Role | Name | Decision | Date | Signature/reference |
|---|---|---|---|---|
| Trading owner | | | | |
| Risk owner | | | | |
| Engineering owner | | | | |

Until every row is complete and this document records explicit approval,
`LIVE_MODE` stays `false`, `REAL_ORDER_CONFIRMATION` stays `false`, and the
platform remains paper-only.
