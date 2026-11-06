"""Run the FastAPI backend (API + paper runner) with configurable host/port.

Usage:
    python -m app

Binds to BACKEND_HOST (default 127.0.0.1) on BACKEND_PORT (default 8000).
Bind 0.0.0.0 explicitly only inside a container or behind a same-host reverse
proxy.
"""

import uvicorn

from app.config.settings import get_settings
from app.logging_config import setup_logging


def main() -> None:
    settings = get_settings()
    settings.validate_startup()
    setup_logging(
        settings.log_level,
        log_file=settings.log_file or None,
        max_bytes=settings.log_max_bytes,
        backup_count=settings.log_backup_count,
    )
    uvicorn.run("app.main:app", host=settings.backend_host, port=settings.backend_port, log_level=settings.log_level.lower())


if __name__ == "__main__":
    main()