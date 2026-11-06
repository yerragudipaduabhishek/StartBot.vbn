"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  fetchResearchExperiments,
  fetchResearchGates,
  fetchResearchPromotions,
  fetchResearchProposals,
  fetchResearchStrategies,
  fetchResearchWalkForward,
  researchAction,
} from "@/lib/api";
import type {
  PromotionRow,
  ResearchExperiment,
  ResearchGates,
  ResearchProposal,
  ResearchWalkForwardRow,
  StrategyVersionRow,
} from "@/lib/types";

const EMPTY_GATES: ResearchGates = { passed: false, failed_reasons: [] };

function fmtDate(value: string | null | undefined): string {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function statusClass(status: string): string {
  if (["promoted", "approved", "passed", "completed"].includes(status)) return "badge-green";
  if (["rejected", "failed", "vetoed", "rolled_back"].includes(status)) return "badge-red";
  if (["canary", "review", "pending", "draft"].includes(status)) return "badge-amber";
  return "badge-muted";
}

function metricSummary(metrics: Record<string, unknown>): string {
  const walk = metrics.walk_forward as Record<string, unknown> | undefined;
  const source = walk ?? metrics;
  const parts = ["accepted_folds", "total_test_trades", "fold_coverage_pct", "mean_test_return_pct"]
    .filter((key) => source[key] !== undefined)
    .map((key) => `${key.replaceAll("_", " ")} ${String(source[key])}`);
  return parts.length ? parts.join(" · ") : "no metrics";
}

function checkRows(gates: ResearchGates) {
  return Object.entries(gates)
    .filter(([key, value]) => !["proposal_id", "passed", "failed_reasons"].includes(key) && value && typeof value === "object")
    .map(([key, value]) => ({ key, value: value as Record<string, unknown> }));
}

export default function ResearchDashboard() {
  const [experiments, setExperiments] = useState<ResearchExperiment[]>([]);
  const [walkForward, setWalkForward] = useState<ResearchWalkForwardRow[]>([]);
  const [proposals, setProposals] = useState<ResearchProposal[]>([]);
  const [strategies, setStrategies] = useState<StrategyVersionRow[]>([]);
  const [promotions, setPromotions] = useState<PromotionRow[]>([]);
  const [gates, setGates] = useState<ResearchGates>(EMPTY_GATES);
  const [selectedId, setSelectedId] = useState("");
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [reviewer, setReviewer] = useState("reviewer@example.com");
  const [findings, setFindings] = useState("");
  const [approver, setApprover] = useState("approver@example.com");
  const [rationale, setRationale] = useState("");
  const [duration, setDuration] = useState(3600);
  const [minTrades, setMinTrades] = useState(20);
  const [canaryTrades, setCanaryTrades] = useState(0);
  const [riskBreaches, setRiskBreaches] = useState(0);
  const [rollbackStrategy, setRollbackStrategy] = useState("strategy_a");
  const [rollbackReason, setRollbackReason] = useState("");

  const selected = useMemo(
    () => proposals.find((row) => row.proposal_id === selectedId) ?? null,
    [proposals, selectedId]
  );

  const load = useCallback(async () => {
    try {
      const [nextExperiments, nextWalkForward, nextProposals, nextStrategies, nextPromotions] = await Promise.all([
        fetchResearchExperiments(),
        fetchResearchWalkForward(),
        fetchResearchProposals(),
        fetchResearchStrategies(),
        fetchResearchPromotions(),
      ]);
      setExperiments(nextExperiments);
      setWalkForward(nextWalkForward);
      setProposals(nextProposals);
      setStrategies(nextStrategies);
      setPromotions(nextPromotions);
      setError("");
      if (nextProposals.length) {
        setSelectedId((current) => current || nextProposals[0].proposal_id);
      }
    } catch (exc) {
      setError(String(exc));
    }
  }, []);

  useEffect(() => {
    const saved = typeof window === "undefined" ? "" : window.sessionStorage.getItem("research-admin-token") ?? "";
    setToken(saved);
    void load();
  }, [load]);

  useEffect(() => {
    if (!selectedId) {
      setGates(EMPTY_GATES);
      return;
    }
    let alive = true;
    fetchResearchGates(selectedId)
      .then((next) => {
        if (alive) setGates(next);
      })
      .catch((exc) => {
        if (alive) {
          setGates(EMPTY_GATES);
          setError(String(exc));
        }
      });
    return () => {
      alive = false;
    };
  }, [selectedId]);

  function saveToken(value: string) {
    setToken(value);
    if (typeof window !== "undefined") {
      window.sessionStorage.setItem("research-admin-token", value);
    }
  }

  async function run(path: string, body: Record<string, unknown>, success: string) {
    if (!token) {
      setError("Enter the admin token to perform governance actions.");
      return;
    }
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await researchAction(path, token, body);
      setNotice(success);
      await load();
    } catch (exc) {
      setError(String(exc));
    } finally {
      setBusy(false);
    }
  }

  const activeProposalId = selected?.proposal_id ?? "";

  return (
    <main>
      <div className="research-head">
        <div>
          <h1>Research &amp; Governance</h1>
          <p className="muted">
            Read-only research evidence, gate truth and human approval trail. AI output can never promote or execute a
            strategy; every mutation below requires the admin token and stored gates.
          </p>
        </div>
        <span className="badge badge-green">PAPER ONLY</span>
      </div>

      <div className="research-token">
        <label>
          Admin token (session only)
          <input
            type="password"
            value={token}
            onChange={(event) => saveToken(event.target.value)}
            placeholder="X-Admin-Token"
            autoComplete="off"
          />
        </label>
        <button className="btn" onClick={() => void load()} disabled={busy}>
          Refresh
        </button>
      </div>

      {error && <div className="risk-banner">{error}</div>}
      {notice && <div className="research-notice">{notice}</div>}

      <section className="research-grid">
        <div className="research-panel">
          <h2>Experiments</h2>
          <div className="research-scroll">
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Strategy</th>
                  <th>Dataset</th>
                  <th>Status</th>
                  <th>Metrics</th>
                  <th>Created</th>
                </tr>
              </thead>
              <tbody>
                {experiments.length === 0 && (
                  <tr><td colSpan={6} className="muted">No recorded experiments yet.</td></tr>
                )}
                {experiments.map((row) => (
                  <tr key={row.experiment_id}>
                    <td className="mono short">{row.experiment_id.slice(0, 12)}</td>
                    <td>{row.strategy_id} <span className="muted">v{row.strategy_version}</span></td>
                    <td className="mono short">{row.dataset_hash.slice(0, 12) || "-"}</td>
                    <td><span className={`badge ${statusClass(row.status)}`}>{row.status}</span></td>
                    <td>{metricSummary(row.metrics)}</td>
                    <td className="muted">{fmtDate(row.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <h2 className="research-subhead">Walk-forward runs</h2>
          <div className="research-scroll">
            <table>
              <thead>
                <tr>
                  <th>Run</th>
                  <th>Strategy</th>
                  <th>Folds</th>
                  <th>Aggregate</th>
                  <th>Artifact</th>
                  <th>Created</th>
                </tr>
              </thead>
              <tbody>
                {walkForward.length === 0 && (
                  <tr><td colSpan={6} className="muted">No persisted walk-forward runs yet.</td></tr>
                )}
                {walkForward.map((row) => (
                  <tr key={row.run_id}>
                    <td className="mono short">{row.run_id.slice(0, 12)}</td>
                    <td>{row.strategy_id || "-"}</td>
                    <td>{row.folds}</td>
                    <td>{metricSummary(row.aggregate)}</td>
                    <td className="mono short">{row.artifact_path || "-"}</td>
                    <td className="muted">{fmtDate(row.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <div className="research-panel">
          <h2>Proposals</h2>
          <div className="research-scroll">
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Strategy</th>
                  <th>Status</th>
                  <th>Updated</th>
                </tr>
              </thead>
              <tbody>
                {proposals.length === 0 && (
                  <tr><td colSpan={4} className="muted">No proposals yet.</td></tr>
                )}
                {proposals.map((row) => (
                  <tr
                    key={row.proposal_id}
                    className={row.proposal_id === selectedId ? "research-selected" : ""}
                    onClick={() => setSelectedId(row.proposal_id)}
                  >
                    <td className="mono short">{row.proposal_id.slice(0, 12)}</td>
                    <td>{row.strategy_id} <span className="muted">v{row.strategy_version}</span></td>
                    <td><span className={`badge ${statusClass(row.status)}`}>{row.status}</span></td>
                    <td className="muted">{fmtDate(row.updated_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <section className="research-panel">
        <h2>Gate truth {selected ? <span className="muted">· {selected.proposal_id}</span> : null}</h2>
        {!selected && <p className="muted">Select a proposal to inspect its gates.</p>}
        {selected && (
          <>
            <div className="research-gate-summary">
              <span className={`badge ${gates.passed ? "badge-green" : "badge-red"}`}>
                {gates.passed ? "ALL GATES PASSED" : "BLOCKED"}
              </span>
              <span className="muted">dataset {selected.dataset_hash.slice(0, 16) || "-"} · artifact {selected.artifact_path || "-"}</span>
            </div>
            {gates.failed_reasons?.length > 0 && (
              <ul className="research-reasons">
                {gates.failed_reasons.map((reason) => <li key={reason}>{reason}</li>)}
              </ul>
            )}
            <div className="gate-grid">
              {checkRows(gates).map(({ key, value }) => (
                <div key={key} className={`gate-card ${value.passed ? "gate-pass" : "gate-fail"}`}>
                  <span className="gate-name">{key.replaceAll("_", " ")}</span>
                  <span className={`badge ${value.passed ? "badge-green" : "badge-red"}`}>
                    {value.passed ? "PASS" : "FAIL"}
                  </span>
                  {typeof value.reason === "string" && value.reason && <span className="muted">{value.reason}</span>}
                </div>
              ))}
            </div>
          </>
        )}
      </section>

      <section className="research-grid">
        <div className="research-panel">
          <h2>Human actions {activeProposalId ? <span className="muted">· {activeProposalId.slice(0, 12)}</span> : null}</h2>
          <div className="action-grid">
            <div className="action-card">
              <h3>Add review</h3>
              <label>Reviewer<input value={reviewer} onChange={(event) => setReviewer(event.target.value)} /></label>
              <label>Findings (one per line)<textarea value={findings} onChange={(event) => setFindings(event.target.value)} rows={3} /></label>
              <button
                className="btn"
                disabled={busy || !activeProposalId}
                onClick={() =>
                  void run(
                    `research/proposals/${activeProposalId}/reviews`,
                    { reviewer, approved: true, findings: findings.split("\n").filter(Boolean) },
                    "Review recorded."
                  )
                }
              >
                Record approval review
              </button>
            </div>

            <div className="action-card">
              <h3>Approve</h3>
              <label>Approver<input value={approver} onChange={(event) => setApprover(event.target.value)} /></label>
              <label>Rationale<input value={rationale} onChange={(event) => setRationale(event.target.value)} /></label>
              <button
                className="btn"
                disabled={busy || !activeProposalId}
                onClick={() => void run(`research/proposals/${activeProposalId}/approve`, { approver, rationale }, "Proposal approved.")}
              >
                Approve proposal
              </button>
            </div>

            <div className="action-card">
              <h3>Canary</h3>
              <label>Duration (seconds)<input type="number" value={duration} onChange={(event) => setDuration(Number(event.target.value))} /></label>
              <label>Min trades<input type="number" value={minTrades} onChange={(event) => setMinTrades(Number(event.target.value))} /></label>
              <button
                className="btn"
                disabled={busy || !activeProposalId}
                onClick={() => void run(`research/proposals/${activeProposalId}/canary/start`, { duration_seconds: duration, min_trades: minTrades }, "Canary started.")}
              >
                Start canary
              </button>
              <label>Trades<input type="number" value={canaryTrades} onChange={(event) => setCanaryTrades(Number(event.target.value))} /></label>
              <label>Risk breaches<input type="number" value={riskBreaches} onChange={(event) => setRiskBreaches(Number(event.target.value))} /></label>
              <button
                className="btn"
                disabled={busy || !activeProposalId}
                onClick={() =>
                  void run(
                    `research/proposals/${activeProposalId}/canary/record`,
                    { trades: canaryTrades, risk_breaches: riskBreaches, reconciliation_ok: true, alerts_ok: true },
                    "Canary evidence recorded."
                  )
                }
              >
                Record evidence
              </button>
              <button
                className="btn"
                disabled={busy || !activeProposalId}
                onClick={() => void run(`research/proposals/${activeProposalId}/canary/finish`, {}, "Canary finished.")}
              >
                Finish canary
              </button>
            </div>

            <div className="action-card">
              <h3>Promote / rollback</h3>
              <button
                className="btn"
                disabled={busy || !activeProposalId}
                onClick={() => void run(`research/proposals/${activeProposalId}/promote`, { approver }, "Strategy promoted after stored gates.")}
              >
                Promote strategy
              </button>
              <label>Strategy<input value={rollbackStrategy} onChange={(event) => setRollbackStrategy(event.target.value)} /></label>
              <label>Rollback reason<input value={rollbackReason} onChange={(event) => setRollbackReason(event.target.value)} /></label>
              <button
                className="btn btn-danger"
                disabled={busy}
                onClick={() => void run(`research/strategies/${rollbackStrategy}/rollback`, { reason: rollbackReason }, "Strategy rolled back.")}
              >
                Roll back strategy
              </button>
            </div>
          </div>
        </div>

        <div className="research-panel">
          <h2>Strategy versions</h2>
          <div className="research-scroll">
            <table>
              <thead>
                <tr>
                  <th>Strategy</th>
                  <th>Version</th>
                  <th>Status</th>
                  <th>Active</th>
                  <th>Promoted</th>
                </tr>
              </thead>
              <tbody>
                {strategies.length === 0 && (
                  <tr><td colSpan={5} className="muted">No strategy versions yet.</td></tr>
                )}
                {strategies.map((row) => (
                  <tr key={`${row.strategy_id}-${row.version}`}>
                    <td>{row.strategy_id}</td>
                    <td className="mono short">{row.version}</td>
                    <td><span className={`badge ${statusClass(row.status)}`}>{row.status}</span></td>
                    <td>{row.active ? <span className="badge badge-green">ACTIVE</span> : <span className="muted">no</span>}</td>
                    <td className="muted">{fmtDate(row.promoted_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <h2 className="research-subhead">Promotion history</h2>
          <div className="research-scroll">
            <table>
              <thead>
                <tr>
                  <th>Strategy</th>
                  <th>Version</th>
                  <th>Previous</th>
                  <th>Status</th>
                  <th>Promoted by</th>
                  <th>Rolled back</th>
                </tr>
              </thead>
              <tbody>
                {promotions.length === 0 && (
                  <tr><td colSpan={6} className="muted">No promotions yet.</td></tr>
                )}
                {promotions.map((row) => (
                  <tr key={`${row.proposal_id}-${row.promoted_at}`}>
                    <td>{row.strategy_id}</td>
                    <td className="mono short">{row.strategy_version}</td>
                    <td className="muted">{row.previous_version ?? "-"}</td>
                    <td><span className={`badge ${statusClass(row.status)}`}>{row.status}</span></td>
                    <td>{row.promoted_by ?? "-"}</td>
                    <td className="muted">{row.rolled_back_at ? `${fmtDate(row.rolled_back_at)} · ${row.rollback_reason ?? ""}` : "-"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </section>
    </main>
  );
}
