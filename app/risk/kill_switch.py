"""Persistent kill switch for new entries.

The switch is intentionally separate from risk drawdown halts: a human can
halt immediately, and the switch survives restarts. It only blocks new entries;
existing paper positions continue to be managed/closed by the runner.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

from sqlalchemy import select

from app.database.models import KillSwitchState

logger = logging.getLogger(__name__)


class KillSwitch:
    def __init__(self, session_factory=None, account_key: str = "global", clock=None) -> None:
        self.session_factory = session_factory
        self.account_key = account_key
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._halted = False
        self._reason = ""
        self._updated_by = "system"
        if session_factory is not None:
            self._restore()

    def _restore(self) -> None:
        try:
            with self.session_factory() as db:
                row = db.execute(select(KillSwitchState).where(KillSwitchState.account_key == self.account_key)).scalar_one_or_none()
                if row is not None:
                    self._halted = bool(row.halted)
                    self._reason = row.reason or ""
                    self._updated_by = row.updated_by or "system"
        except Exception:
            logger.exception("kill switch: failed to restore state; failing closed")
            self._halted = True
            self._reason = "kill switch state unavailable"

    def is_halted(self) -> bool:
        if self.session_factory is None:
            return self._halted
        try:
            with self.session_factory() as db:
                row = db.execute(select(KillSwitchState).where(KillSwitchState.account_key == self.account_key)).scalar_one_or_none()
                if row is None:
                    return self._halted
                self._halted = bool(row.halted)
                self._reason = row.reason or ""
                self._updated_by = row.updated_by or "system"
                return self._halted
        except Exception:
            logger.exception("kill switch: status check failed; failing closed")
            return True

    def status(self) -> dict:
        halted = self.is_halted()
        return {
            "halted": halted,
            "reason": self._reason,
            "updated_by": self._updated_by,
            "account_key": self.account_key,
        }

    def halt(self, reason: str, actor: str = "cli") -> dict:
        reason = str(reason or "").strip()
        if not reason:
            raise ValueError("halt reason is required")
        self._set(True, reason, actor)
        logger.warning("kill switch HALTED by %s: %s", actor, reason)
        return self.status()

    def resume(self, actor: str = "cli") -> dict:
        self._set(False, "", actor)
        logger.warning("kill switch RESUMED by %s", actor)
        return self.status()

    def _set(self, halted: bool, reason: str, actor: str) -> None:
        self._halted = halted
        self._reason = reason
        self._updated_by = actor
        if self.session_factory is None:
            return
        with self.session_factory() as db:
            row = db.execute(select(KillSwitchState).where(KillSwitchState.account_key == self.account_key)).scalar_one_or_none()
            if row is None:
                row = KillSwitchState(account_key=self.account_key)
                db.add(row)
            row.halted = halted
            row.reason = reason
            row.updated_by = actor
            row.updated_at = self._clock()
            db.commit()
            try:
                from app.observability import record_audit, record_system_event
                action = "halt" if halted else "resume"
                record_audit(
                    db,
                    f"kill_switch_{action}",
                    actor=actor,
                    severity="warning" if halted else "info",
                    payload={"account_key": self.account_key, "reason": reason},
                )
                record_system_event(
                    db,
                    f"kill_switch_{action}",
                    reason or f"kill switch {action}d",
                    severity="warning" if halted else "info",
                    payload={"account_key": self.account_key, "actor": actor},
                )
            except Exception:
                # Observability must never make a safety control fail.
                logger.exception("kill switch: failed to write observability event")


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Halt or resume new trading entries")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--halt", action="store_true")
    group.add_argument("--resume", action="store_true")
    group.add_argument("--status", action="store_true")
    parser.add_argument("--reason", default="")
    parser.add_argument("--confirm", action="store_true", help="required with --resume")
    args = parser.parse_args(argv)

    from app.database.database import SessionLocal, init_db

    init_db()
    switch = KillSwitch(session_factory=SessionLocal)
    if args.status:
        print(switch.status())
        return 0
    if args.halt:
        print(switch.halt(args.reason, actor="cli"))
        return 0
    if not args.confirm:
        print("refusing to resume without --confirm", file=sys.stderr)
        return 2
    print(switch.resume(actor="cli"))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
