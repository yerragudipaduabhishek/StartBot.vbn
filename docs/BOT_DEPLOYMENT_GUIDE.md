# Deployment Guide

## Requirements

The Python project targets Python 3.12. Dependencies are listed in `requirements.txt`; development tools and pytest configuration are in `requirements-dev.txt` and `pyproject.toml`. The dashboard uses Node.js and npm with `dashboard/package-lock.json`.

## Windows local setup

From the repository root:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

Review `.env`. Keep live mode and real-order confirmation disabled and testnet mode enabled. Do not paste credentials into source files or commit `.env`.

Start the backend:

```powershell
python -m app
```

Or run the development server:

```powershell
uvicorn app.main:app --reload
```

Default API: `http://127.0.0.1:8000`. Health: `GET /health`.

## Dashboard setup

```powershell
cd dashboard
npm ci
npm run dev
```

Configure the dashboard backend URL as appropriate for local development (README documents `BACKEND_URL=http://127.0.0.1:8000`). The dashboard development server normally runs on port 3000.

## Docker Compose

The compose file defines:
- `app`: Python backend;
- `dashboard`: Next.js dashboard;
- `soak`: optional paper-only soak profile;
- `db` and `redis`: optional infrastructure profile.

Start backend and dashboard:

```bash
docker compose up --build -d
```

Start optional paper soak:

```bash
docker compose --profile soak up -d soak
```

Start optional Postgres/Redis infrastructure:

```bash
docker compose --profile infra up
```

The compose configuration forces live mode and real-order confirmation off and testnet mode on. Backend and dashboard ports are published to host loopback by default. SQLite files and logs persist through `./data` and `./logs` mounts.

For Linux hosts, create and set ownership of data/log directories for the non-root container UID described in README. Do not expose the backend port directly to the public internet.

## Databases and backups

Default trading database: `sqlite:///./data/trading.db`.  
Default research database: `sqlite:///./data/research.db`.

These must remain separate. The README documents backup invocation:

```bash
python -m app.ops.backup --source data/trading.db --source data/research.db --dest backups/
```

Perform restore drills on copies, not over the active database.

## Tests and research commands

```bash
pytest -q
python -m app.backtest.study --mode baseline --no-record
python -m app.backtest.study --mode baseline
python -m app.backtest.study --mode walk
python -m app.backtest.study --mode walk --refit
```

A testnet execution sweep is an explicit separate activity and may require credentials. Do not run order-placement options unless intentionally validating on Delta testnet with testnet credentials and operator oversight.

## Environment variables

Important groups in `.env.example` / `app/config/settings.py` include:
- application/environment/logging;
- backend bind, port, admin token, CORS, rate limit and body cap;
- Delta region, API keys and data URL override;
- symbol, timeframe and history limit;
- live/testnet/real-order safety flags;
- testnet execution polling/journal/state paths;
- trading and research database URLs;
- LLM provider, model, network opt-in and budgets;
- news key;
- paper fund, risk, fee, slippage, contract value and symbol.

Always inspect the actual `.env.example` in the checked-out revision before deployment; this document summarizes categories rather than replacing the file.
