"""Alembic migration environment for AlphaLens v8.

Reads the database URL from ``NEON_DIRECT_URL`` (L12: Direct URL for migrations,
not the pooled app URL). No URL is stored in ``alembic.ini``.

Migrations are hand-written with explicit ``op.execute`` DDL, so there is no
``target_metadata`` / autogenerate support — ``target_metadata`` stays ``None``.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

from alembic import context

# Alembic Config object — provides access to alembic.ini values.
config = context.config

# Configure Python logging from the ini file, if present.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# No ORM metadata: we drive schema changes via explicit SQL, not autogenerate.
target_metadata = None


def _database_url() -> str:
    """Return the migration database URL from NEON_DIRECT_URL (loads .env first)."""
    load_dotenv()
    url = os.environ.get("NEON_DIRECT_URL")
    if not url:
        raise RuntimeError(
            "NEON_DIRECT_URL is not set — required for Alembic migrations "
            "(L12: Direct URL, not the pooled app URL)."
        )
    return url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emit SQL to stdout, no DBAPI connection."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode — open a connection and run against the DB."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
