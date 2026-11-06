from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config.settings import get_settings
import app.main as main

REPO = Path(__file__).resolve().parents[1]


class FakeMarket:
    def __init__(self, data_status: str, candles_count: int) -> None:
        self._status = data_status
        self._count = candles_count

    def snapshot(self) -> dict:
        return {"data_status": self._status, "candles_count": self._count}


def test_settings_bind_defaults() -> None:
    settings = get_settings()
    assert settings.backend_host == "127.0.0.1"
    assert settings.backend_port == 8000
    assert settings.admin_token == ""
    assert settings.live_mode is False
    assert settings.testnet_mode is True
    assert settings.real_order_confirmation is False


def test_settings_bind_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BACKEND_HOST", "127.0.0.1")
    monkeypatch.setenv("BACKEND_PORT", "9123")
    get_settings.cache_clear()
    try:
        settings = get_settings()
    finally:
        get_settings.cache_clear()
    assert settings.backend_host == "127.0.0.1"
    assert settings.backend_port == 9123
    assert settings.live_mode is False


def test_health_offline_ready_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "state", {})
    client = TestClient(main.app)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["ready"] is False
    assert body["data_status"] is None
    assert body["candles_count"] == 0
    assert body["live_mode"] is False


def test_health_streaming_ready_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        main,
        "state",
        {"market": FakeMarket("streaming", 60)},
    )
    client = TestClient(main.app)
    body = client.get("/health").json()
    assert body["ready"] is True
    assert body["data_status"] == "streaming"
    assert body["candles_count"] == 60
    assert body["symbol"] == "BTCUSDT"


def test_health_does_not_leak_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "state", {})
    client = TestClient(main.app)
    resp = client.get("/health")
    content = resp.text.lower()
    for secret_field in ("api_key", "api_secret", "password", "OPENAI_API_KEY"):
        assert secret_field not in content


def test_health_keys_are_correct(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "state", {})
    client = TestClient(main.app)
    assert set(client.get("/health").json().keys()) == {
        "status",
        "ready",
        "symbol",
        "live_mode",
        "data_status",
        "candles_count",
        "data_source",
        "price_valid",
        "last_candle_age_secs",
        "ws_status",
        "monitor",
        "paper",
    }


def test_docker_compose_has_app_service_and_infra_profile() -> None:
    compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    assert "app:" in compose
    assert "build: ." in compose
    assert "${BACKEND_PORT:-8000}:8000" in compose
    assert "sqlite:////app/data/trading.db" in compose
    assert "profiles: [\"infra\"]" in compose
    assert "restart: unless-stopped" in compose


def test_dockerfile_binds_and_runs_python_dash_m_app() -> None:
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    assert "WORKDIR /app" in dockerfile
    assert "EXPOSE 8000" in dockerfile
    assert '"python", "-m", "app"' in dockerfile
    assert "FROM python" in dockerfile


def test_env_example_documents_backend_bind() -> None:
    env = (REPO / ".env.example").read_text(encoding="utf-8")
    assert "BACKEND_HOST=127.0.0.1" in env
    assert "BACKEND_PORT=8000" in env
    assert "ADMIN_TOKEN=" in env
    assert "BACKEND_URL" in env


def test_dashboard_url_configurable() -> None:
    cfg = (REPO / "dashboard" / "next.config.mjs").read_text(encoding="utf-8")
    assert "process.env.BACKEND_URL" in cfg
    assert "http://127.0.0.1:8000" in cfg
    dash_env = (REPO / "dashboard" / ".env.example").read_text(encoding="utf-8")
    assert "BACKEND_URL" in dash_env


def test_readme_documents_run_and_deploy() -> None:
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "python -m app" in readme
    assert "BACKEND_URL" in readme
    assert "docker compose up --build -d" in readme
    assert "/health" in readme


def test_env_files_ignored_by_git() -> None:
    excludes = (REPO / ".gitignore").read_text(encoding="utf-8")
    assert "\n.env" in excludes or ".env" in excludes