from __future__ import annotations

import logging
from pathlib import Path

import psycopg

from app.config import get_settings
from app.logging import configure_logging


logger = logging.getLogger(__name__)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    migration_root = Path(__file__).resolve().parents[1] / "migrations"
    migrations = sorted(migration_root.glob("*.sql"))
    if not migrations:
        raise RuntimeError(f"No SQL migrations found in {migration_root}")
    with psycopg.connect(
        settings.postgres_dsn.get_secret_value(),
        autocommit=True,
        connect_timeout=settings.postgres_connect_timeout_seconds,
    ) as connection:
        with connection.cursor() as cursor:
            for migration in migrations:
                logger.info("migration_started", extra={"event": {"migration": migration.name}})
                cursor.execute(migration.read_text(encoding="utf-8"), prepare=False)
                logger.info("migration_completed", extra={"event": {"migration": migration.name}})


if __name__ == "__main__":
    main()
