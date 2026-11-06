"""Phase 7 security middleware, startup validation, backups and log rotation."""

from __future__ import annotations

import io
import json
import logging
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.config.settings import Settings
from app.logging_config import setup_logging
from app.ops.backup import (
    BackupError,
    backup_database,
    restore_database,
    verify_database,
)


@pytest.fixture(autouse=True)
def _clear_rate_limits():
    main.reset_rate_limits()
    yield
    main.reset_rate_limits()


def _settings(**overrides) -> Settings:
    values = {
        "environment": "staging",
        "admin_token": "x" * 32,
        "backend_host": "127.0.0.1",
        "testnet_mode": True,
        "live_mode": False,
        "real_order_confirmation": False,
        "cors_origins": "https://dashboard.example.com",
        "database_url": "sqlite:///trading.db",
        "research_database_url": "sqlite:///research.db",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_startup_validation_rejects_live_mode_and_incomplete_production():
    live = _settings(live_mode=True)
    assert live.startup_safety_problems()
    with pytest.raises(RuntimeError, match="LIVE_MODE"):
        live.validate_startup()

    staging = _settings(admin_token="")
    with pytest.raises(RuntimeError, match="ADMIN_TOKEN"):
        staging.validate_startup()

    shared = _settings(research_database_url="sqlite:///trading.db")
    assert any("RESEARCH_DATABASE_URL" in problem for problem in shared.startup_safety_problems())

    wildcard = _settings(cors_origins="*")
    assert any("CORS_ORIGINS" in problem for problem in wildcard.startup_safety_problems())

    assert _settings().startup_safety_problems() == []
    _settings().validate_startup()


def test_dev_profile_keeps_mutations_closed_but_boots():
    dev = Settings(_env_file=None, environment="dev", admin_token="")
    assert dev.startup_safety_problems() == []
    dev.validate_startup()


def test_middleware_enforces_body_limit_and_rate_limit(monkeypatch):
    monkeypatch.setattr(main, "settings", main.settings.model_copy(update={"max_request_bytes": 20}))
    client = TestClient(main.app)
    response = client.post("/research/proposals", content=b"x" * 50)
    assert response.status_code == 413

    monkeypatch.setattr(main, "settings", main.settings.model_copy(update={"rate_limit_per_minute": 2}))
    main.reset_rate_limits()
    assert client.get("/safety").status_code == 200
    assert client.get("/safety").status_code == 200
    blocked = client.get("/safety")
    assert blocked.status_code == 429
    assert blocked.headers["Retry-After"] == "60"
    assert blocked.headers["X-Content-Type-Options"] == "nosniff"

    main.reset_rate_limits()
    assert client.get("/health").status_code == 200
    assert client.get("/health").status_code == 200


def test_backup_restore_round_trip_with_integrity_checks(tmp_path):
    database = tmp_path / "trading.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE paper_account (id INTEGER PRIMARY KEY, balance REAL)")
        connection.execute("INSERT INTO paper_account (balance) VALUES (5000)")
        connection.commit()

    backup = backup_database(database, tmp_path / "backups" / "snapshot.db")
    assert verify_database(backup)["ok"] is True
    with pytest.raises(BackupError, match="refusing to overwrite"):
        backup_database(database, backup)

    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE paper_account SET balance = 1")
        connection.commit()

    restore_database(backup, database, overwrite=True)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT balance FROM paper_account").fetchone()[0] == 5000
    assert (tmp_path / "trading.db.pre-restore").is_file()

    with pytest.raises(BackupError, match="target exists"):
        restore_database(backup, database)


def test_backup_cli_reports_missing_source(capsys, tmp_path):
    from app.ops.backup import main as backup_main

    assert backup_main(["--source", str(tmp_path / "missing.db"), "--dest", str(tmp_path)]) == 2
    assert "backup error" in capsys.readouterr().err


def test_json_logging_rotates_files(tmp_path):
    log_file = tmp_path / "logs" / "app.log"
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    try:
        setup_logging("INFO", stream=io.StringIO(), log_file=str(log_file), max_bytes=300, backup_count=2)
        logger = logging.getLogger("test.rotation")
        for index in range(40):
            logger.info("rotation probe %s %s", index, "y" * 30)
        for handler in root.handlers:
            handler.flush()
        assert log_file.is_file()
        assert (tmp_path / "logs" / "app.log.1").is_file()
        record = log_file.read_text(encoding="utf-8").splitlines()[-1]
        assert json.loads(record)["level"] == "INFO"
    finally:
        for handler in list(root.handlers):
            handler.close()
        root.handlers.clear()
        root.handlers.extend(saved_handlers)
        root.setLevel(saved_level)
