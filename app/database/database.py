import logging
import os
from collections.abc import Generator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config.settings import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()


class Base(DeclarativeBase):
    pass


def _ensure_sqlite_dir(url: str) -> None:
    if url.startswith("sqlite:///"):
        path = url.replace("sqlite:///", "", 1)
        if path and path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)


_extra: dict = {}
if settings.database_url.startswith("sqlite"):
    _extra["connect_args"] = {"check_same_thread": False}

_ensure_sqlite_dir(settings.database_url)
engine = create_engine(settings.database_url, **{"echo": False, **_extra})


if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def _migrate_sqlite_columns() -> None:
    """Add columns introduced after the table was first created.

    create_all() does not alter existing tables; SQLite ALTER TABLE only adds
    columns. New tables (paper_account / paper_positions) are created by
    create_all(); only the pre-existing paper_trades table needs the extra
    columns below. This keeps an existing dev/VPS DB usable without a reset.
    """
    if not settings.database_url.startswith("sqlite"):
        return
    additions = {
        "paper_trades": {
            "take_profit": "FLOAT",
            "notional": "FLOAT DEFAULT 0.0",
            "leverage": "FLOAT DEFAULT 0.0",
            "fees": "FLOAT DEFAULT 0.0",
            "slippage": "FLOAT DEFAULT 0.0",
        },
        "signals": {
            "strategy_id": "VARCHAR(64)",
            "strategy_version": "VARCHAR(32)",
            "risk_reason": "TEXT",
            "data_quality": "TEXT",
        },
        "candles": {
            "source": "VARCHAR(32) DEFAULT 'unknown'",
            "venue": "VARCHAR(64) DEFAULT 'unknown'",
            "transport": "VARCHAR(16) DEFAULT 'unknown'",
            "fallback_used": "BOOLEAN DEFAULT 0",
            "is_closed": "BOOLEAN DEFAULT 1",
            "quality_status": "VARCHAR(16) DEFAULT 'ok'",
            "quality_flags": "TEXT DEFAULT ''",
            "ingested_at": "DATETIME",
            "fetched_at": "DATETIME",
        },
    }
    inspector = inspect(engine)
    existing = {t for t in inspector.get_table_names()}
    for table, columns in additions.items():
        if table not in existing:
            continue
        present = {c["name"] for c in inspector.get_columns(table)}
        with engine.begin() as conn:
            for name, dtype in columns.items():
                if name not in present:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {dtype}"))
                    logger.info("migrated %s: added column %s", table, name)


def init_db() -> None:
    from app.database import models  # noqa: F401  (register tables)

    _migrate_sqlite_columns()
    Base.metadata.create_all(bind=engine)
    logger.info("database ready at %s", settings.database_url)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
