"""Soak-test telemetry: bounded in-memory buffer + append-only JSONL writer.

Each soak cycle appends one row per watchlist symbol. Rows are kept in a ring
buffer for live inspection and flushed to a JSONL file (default
data/soak_telemetry.jsonl) so a crash never loses the last tick and a human can
tail the run afterwards.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def iso_now(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc).isoformat()


class SoakTelemetry:
    def __init__(self, out_path: str | None = None, max_rows: int = 5000) -> None:
        self.out_path = out_path
        self.max_rows = max_rows
        self.rows: list[dict] = []  # bounded ring buffer (latest N rows)
        self.rows_written = 0
        self._pending: list[dict] = []  # rows not yet written to disk
        self._fh = None
        if out_path:
            parent = os.path.dirname(os.path.abspath(out_path))
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            self._fh = open(out_path, "a", encoding="utf-8")

    def add(self, row: dict) -> None:
        row = dict(row)
        row.setdefault("timestamp", iso_now())
        self.rows.append(row)
        self._pending.append(row)
        if len(self.rows) > self.max_rows:
            self.rows.pop(0)

    def flush(self) -> None:
        if self._fh is None or not self._pending:
            return
        for row in self._pending:
            self._fh.write(json.dumps(row, default=str) + "\n")
        self.rows_written += len(self._pending)
        self._pending = []
        self._fh.flush()

    def close(self) -> None:
        try:
            self.flush()
        finally:
            if self._fh is not None:
                self._fh.close()
                self._fh = None

    def latest(self) -> dict | None:
        return self.rows[-1] if self.rows else None