"""Execution report model, slippage math and the audit journal.

The report answers the 6 validation questions (entry/exit timing, SL/TP,
slippage+fees, failure behaviour) with one struct that is persisted as JSONL,
CSV and a DB row so the testnet phase is auditable end to end.
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def slippage_between(expected: float | None, actual: float | None, side: str) -> tuple[float, float]:
    """Return (signed_diff, bps). Positive diff/bps is ALWAYS the worse fill:
    for a long, an actual above expected hurts; for a short, an actual below
    expected hurts. Returns zeros when either price is missing."""
    if expected is None or actual is None or expected == 0:
        return 0.0, 0.0
    raw = actual - expected
    signed = raw if side == "long" else -raw
    bps = signed / expected * 10_000.0
    return signed, bps


def execution_report_from_order(
    *,
    signal_time: int,
    side: str,
    size: float,
    expected_entry: float,
    result,
    expected_sl: float | None = None,
    expected_tp: float | None = None,
    venue: str = "delta-testnet",
    errors: list[str] | None = None,
) -> ExecutionReport:
    """Build a report from an OrderResult; slippage is derived from prices."""
    report = ExecutionReport(
        signal_time=signal_time,
        side=side,
        size=size,
        expected_entry=expected_entry,
        actual_entry=result.fill_price if getattr(result, "fill_price", None) else getattr(result, "price", None),
        expected_sl=expected_sl,
        expected_tp=expected_tp,
        fees=float(getattr(result, "fees", 0.0) or 0.0),
        latency_ms=float(getattr(result, "latency_ms", 0.0) or 0.0),
        order_status=getattr(result, "order_state", "open") or "open",
        client_order_id=getattr(result, "client_order_id", "") or "",
        venue=venue,
        errors=list(errors or []),
    )
    report.finish_fill()
    return report


@dataclass
class ExecutionReport:
    signal_time: int
    side: str
    size: float
    expected_entry: float
    actual_entry: float | None = None
    slippage: float = 0.0
    slippage_bps: float = 0.0
    expected_sl: float | None = None
    actual_sl: float | None = None
    expected_tp: float | None = None
    actual_tp: float | None = None
    fees: float = 0.0
    latency_ms: float = 0.0
    order_status: str = "open"
    position_status: str = "flat"
    final_pnl: float = 0.0
    client_order_id: str = ""
    venue: str = "delta-testnet"
    exit_reason: str | None = None
    errors: list[str] = field(default_factory=list)

    def finish_fill(self) -> None:
        """Derive slippage fields from the expected vs actual entry price."""
        self.slippage, self.slippage_bps = slippage_between(self.expected_entry, self.actual_entry, self.side)

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        fill = f"{self.actual_entry:,.2f}" if self.actual_entry is not None else "pending"
        return (
            f"tn[{self.side:<5}] sig={self.signal_time} exp={self.expected_entry:,.2f} "
            f"act={fill} slip={self.slippage_bps:+.1f}bps fees={self.fees:.6f} "
            f"lat={self.latency_ms:.0f}ms order={self.order_status} pos={self.position_status} "
            f"pnl={self.final_pnl:+.2f}"
        )

    def as_db_row(self, symbol: str = "BTCUSDT") -> object:
        from app.database.models import TestnetExecution

        return TestnetExecution(
            client_order_id=self.client_order_id,
            signal_time=self.signal_time,
            symbol=symbol,
            side=self.side,
            size=self.size,
            venue=self.venue,
            expected_entry=self.expected_entry,
            actual_entry=self.actual_entry,
            slippage=self.slippage,
            slippage_bps=self.slippage_bps,
            expected_sl=self.expected_sl,
            actual_sl=self.actual_sl,
            expected_tp=self.expected_tp,
            actual_tp=self.actual_tp,
            fees=self.fees,
            latency_ms=self.latency_ms,
            order_status=self.order_status,
            position_status=self.position_status,
            final_pnl=self.final_pnl,
            exit_reason=self.exit_reason,
            raw=json.dumps(self.to_dict(), default=str),
        )


class ExecutionJournal:
    """Appends JSONL events, execution reports (JSONL + CSV) and DB rows.

    `event()` records the raw order request/response/fill/rejection/error trail;
    `write_report()` persists a completed ExecutionReport. All writes are
    append-only so the audit trail can never be silently rewritten.
    """

    def __init__(self, journal_path: str = "", state_path: str = "", session_factory=None):
        self.journal_path = journal_path
        self.state_path = state_path
        self.session_factory = session_factory

    # --------------------------------------------------------------- events

    def event(self, *, kind: str, **payload) -> None:
        line = json.dumps({"ts": _utcnow_iso(), "kind": kind, **payload}, default=str)
        if self.journal_path:
            try:
                path = self.journal_path if self.journal_path.endswith((".jsonl", ".ndjson")) else self.journal_path + ".jsonl"
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                logger.exception("failed to append journal event")
        logger.info("journal[%s] %s", kind, line)

    # -------------------------------------------------------------- reports

    def write_report(self, report: ExecutionReport, symbol: str = "BTCUSDT") -> None:
        self.event(kind="execution_report", **report.to_dict())
        self._write_csv(report, symbol)
        self._write_db(report, symbol)

    def _write_csv(self, report: ExecutionReport, symbol: str) -> None:
        if not self.journal_path:
            return
        csv_path = self.journal_path.replace(".jsonl", ".csv").replace(".ndjson", ".csv")
        header = list(report.to_dict().keys())
        try:
            exists = __import__("os").path.exists(csv_path) and __import__("os").path.getsize(csv_path) > 0
            with open(csv_path, "a", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore")
                if not exists:
                    writer.writeheader()
                writer.writerow(report.to_dict())
        except OSError:
            logger.exception("failed to append execution CSV")

    def _write_db(self, report: ExecutionReport, symbol: str) -> None:
        """Upsert one row per client_order_id so entry and close updates land
        on the same record while the JSONL/CSV trail keeps both snapshots."""
        try:
            if self.session_factory is not None:
                db = self.session_factory()
            else:
                from app.database.database import SessionLocal

                db = SessionLocal()
            try:
                from app.database.models import TestnetExecution

                row = db.query(TestnetExecution).filter_by(client_order_id=report.client_order_id).first()
                if row is None:
                    row = TestnetExecution(client_order_id=report.client_order_id)
                    db.add(row)
                row.signal_time = report.signal_time
                row.symbol = symbol
                row.side = report.side
                row.size = report.size
                row.venue = report.venue
                row.expected_entry = report.expected_entry
                row.actual_entry = report.actual_entry
                row.slippage = report.slippage
                row.slippage_bps = report.slippage_bps
                row.expected_sl = report.expected_sl
                row.actual_sl = report.actual_sl
                row.expected_tp = report.expected_tp
                row.actual_tp = report.actual_tp
                row.fees = report.fees
                row.latency_ms = report.latency_ms
                row.order_status = report.order_status
                row.position_status = report.position_status
                row.final_pnl = report.final_pnl
                row.exit_reason = report.exit_reason
                row.raw = json.dumps(report.to_dict(), default=str)
                db.commit()
            finally:
                db.close()
        except Exception:
            logger.exception("failed to persist testnet execution row")

    # ----------------------------------------------------------------- reads

    def read_reports(self, limit: int = 50) -> list[dict]:
        if not self.journal_path:
            return []
        path = self.journal_path if self.journal_path.endswith((".jsonl", ".ndjson")) else self.journal_path + ".jsonl"
        rows: list[dict] = []
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    if obj.get("kind") == "execution_report":
                        rows.append(obj)
        except OSError:
            return []
        return rows[-limit:]