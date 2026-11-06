"""Consistent SQLite backup/restore using the online backup API.

SQLite files must never be copied while a writer is active. These helpers use
``sqlite3.Connection.backup`` for a consistent snapshot, verify integrity before
and after restore, and keep a pre-restore copy so an operator can roll back a
bad restore.

Usage:
    python -m app.ops.backup --source data/trading.db --dest backups/
    python -m app.ops.backup --source data/trading.db --source data/research.db --dest backups/
    python -m app.ops.backup --restore backups/trading-20260101T000000Z.db --target data/trading.db --overwrite
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


class BackupError(RuntimeError):
    """A backup or restore precondition failed."""


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    return sqlite3.connect(str(path))


def verify_database(path: str | Path) -> dict:
    database = Path(path)
    if not database.is_file():
        raise BackupError(f"database not found: {database}")
    with closing(_connect(database, read_only=True)) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        tables = connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
    return {"ok": integrity == "ok", "integrity": integrity, "tables": int(tables), "path": str(database)}


def backup_database(source: str | Path, destination: str | Path) -> Path:
    """Write a consistent snapshot and return its path.

    If ``destination`` is an existing directory (or ends with a separator),
    a UTC timestamped filename is generated.
    """
    source_path = Path(source)
    if not source_path.is_file():
        raise BackupError(f"database not found: {source_path}")
    destination_path = Path(destination)
    if destination_path.is_dir() or str(destination).endswith(("/", "\\")):
        destination_path.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination_path = destination_path / f"{source_path.stem}-{stamp}.db"
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    if destination_path.exists():
        raise BackupError(f"refusing to overwrite existing backup: {destination_path}")

    with closing(_connect(source_path, read_only=True)) as source_conn, closing(_connect(destination_path)) as destination_conn:
        source_conn.backup(destination_conn)
    verification = verify_database(destination_path)
    if not verification["ok"]:
        destination_path.unlink(missing_ok=True)
        raise BackupError(f"backup failed integrity check: {verification['integrity']}")
    return destination_path


def restore_database(backup: str | Path, target: str | Path, *, overwrite: bool = False) -> Path:
    """Verify a backup, keep a pre-restore copy, then atomically replace target."""
    backup_path = Path(backup)
    target_path = Path(target)
    verification = verify_database(backup_path)
    if not verification["ok"]:
        raise BackupError(f"backup failed integrity check: {verification['integrity']}")
    if target_path.exists():
        if not overwrite:
            raise BackupError(f"target exists (pass --overwrite): {target_path}")
        pre_restore = target_path.with_name(target_path.name + ".pre-restore")
        if pre_restore.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            pre_restore = target_path.with_name(f"{target_path.name}.pre-restore-{stamp}")
        backup_database(target_path, pre_restore)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    # Restore through SQLite's backup API instead of replacing the file. On
    # Windows an open handle (API process, pooled connection) blocks
    # os.replace; the online API writes safely through SQLite's locking.
    with closing(_connect(backup_path, read_only=True)) as source_conn, closing(_connect(target_path)) as target_conn:
        source_conn.backup(target_conn)
    restored = verify_database(target_path)
    if not restored["ok"]:
        raise BackupError(f"restored database failed integrity check: {restored['integrity']}")
    return target_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SQLite backup/restore with integrity checks")
    parser.add_argument("--source", action="append", default=[], help="source database (repeatable)")
    parser.add_argument("--dest", help="backup file or directory")
    parser.add_argument("--restore", help="backup file to restore")
    parser.add_argument("--target", help="restore destination database")
    parser.add_argument("--overwrite", action="store_true", help="allow replacing an existing restore target")
    args = parser.parse_args(argv)

    try:
        if args.restore:
            if not args.target:
                raise BackupError("--target is required with --restore")
            restored = restore_database(args.restore, args.target, overwrite=args.overwrite)
            print(json.dumps({"restored": str(restored), "verification": verify_database(restored)}, indent=2))
            return 0
        if not args.source or not args.dest:
            raise BackupError("provide --source/--dest, or --restore/--target")
        written = [str(backup_database(source, args.dest)) for source in args.source]
        print(json.dumps({"backups": written}, indent=2))
        return 0
    except BackupError as exc:
        print(f"backup error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
