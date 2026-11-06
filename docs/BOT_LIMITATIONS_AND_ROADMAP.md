# Limitations and Roadmap

## Current release boundary

The repository is a paper-trading and research platform. Live execution is disabled in code and the live-readiness review explicitly says NOT APPROVED. The system should be used for local development, historical research, paper simulation and carefully controlled Delta testnet validation only.

## Known evidence gaps

The current live-readiness review identifies:
- no completed continuous 30-day / 100-trade field soak;
- no named human live approver or signed readiness record;
- no recent credentialed testnet round-trip recorded in the review;
- no independent external penetration test;
- backup restore and on-call rehearsal evidence still required;
- legal/venue production API terms and account permissions not reviewed.

These are readiness gaps, not documentation formalities.

## Model and strategy limitations

- A backtest is not a promise of future performance.
- Paper fills model fees and adverse slippage but cannot fully reproduce exchange queue position, partial fills, funding, latency, outages or liquidation.
- Strategy A is locked to BTCUSDT 1h; changing symbol/timeframe requires a new strategy/profile and independent validation.
- The sentiment agent may operate neutrally when headlines are absent or VADER is unavailable.
- Derivatives scores depend on availability and quality of funding/open-interest fields.
- Multi-agent scoring is a deterministic weighted ensemble, not autonomous general intelligence.
- A strategy scan can overfit; use chronological out-of-sample and walk-forward artifacts.

## Operational limitations

- SQLite is the default single-node persistence choice; it is not a substitute for a highly available production data platform.
- The API rate limiter is in-process and is not a distributed rate-limit service.
- A paper kill switch blocks new entries; it does not automatically flatten an open position.
- Public REST fallback is for market-data reads only; operators must inspect provenance and fallback labels.
- Testnet behavior is not production execution evidence.
- A running dashboard does not mean the data feed is healthy or the strategy is approved.

## Recommended roadmap

### Priority 0 — Preserve current safety boundary
- Keep live mode disabled and production order hosts blocked.
- Keep tests offline and free of real orders.
- Preserve strict risk and research/execution import boundaries.

### Priority 1 — Complete evidence and operational drills
- Run and archive the required continuous paper soak with trade count, incidents, reconciliation and telemetry.
- Produce reproducible walk-forward artifacts and human review.
- Complete testnet round-trip, reconnect/recovery and order reconciliation evidence.
- Perform backup restore drill and on-call/incident rehearsal.
- Review dependency and container security, secret scanning and TLS reverse-proxy configuration.

### Priority 2 — Improve operational resilience
- Add alert-delivery integration and heartbeat/dead-man alerts.
- Add distributed rate limiting if multi-instance deployment is introduced.
- Add database migration/versioning and tested backup retention.
- Improve structured audit correlation across signals, risk decisions, orders, fills and config changes.
- Add external monitoring and deployment health checks.

### Priority 3 — Only after separate governance review
Any production/live work must be a separate reviewed engineering change with venue-specific safeguards, independent risk review, signed human approval, limited capital and rollback plan. No LLM or research agent may authorize it.

## Change-management checklist

For each strategy/risk/execution change:
1. Update the profile or contract intentionally.
2. Add tests for normal and rejection/failure paths.
3. Run the complete offline test suite.
4. Produce research artifacts when strategy behavior changes.
5. Update documentation and operational runbooks.
6. Preserve human approval and rollback evidence.
