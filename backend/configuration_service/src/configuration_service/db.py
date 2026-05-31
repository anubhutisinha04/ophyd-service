"""
PostgreSQL persistence layer for configuration_service.

Defines the shared SQLAlchemy Core schema (one MetaData for all tables) and the
engine factory. ``device_registry_store`` and ``standalone_pv_store`` build their
queries against these Table objects; ``main.py`` owns the single Engine (created
once at startup) and injects it into both stores.

Schema notes:
- Timestamps are epoch floats (``Float`` -> DOUBLE PRECISION) to preserve the
  existing wire/API shape.
- ``device_audit_log.id`` is a BIGINT IDENTITY column: a monotonic, gap-tolerant
  sequence. It is the change-feed cursor that bluesky-queueserver Layer 2 polls
  via ``GET /api/v1/devices/changes?since_version=N`` — never reuse or reset it.
  BIGINT (not INT) because it grows on every seed/CRUD/reset/lock event (lock
  events log one row per affected device), so a 32-bit range could be exhausted.
- ``standalone_pvs.labels`` is a JSON-encoded TEXT column (parsed in Python) to
  keep the existing wire/API shape; JSONB is a possible future tightening.
"""

from sqlalchemy import (
    BigInteger,
    Column,
    Float,
    Identity,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
)
from sqlalchemy.engine import Engine

metadata = MetaData()

device_registry = Table(
    "device_registry",
    metadata,
    Column("name", String, primary_key=True),
    Column("device_metadata", Text, nullable=False),
    Column("instantiation_spec", Text),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)

device_audit_log = Table(
    "device_audit_log",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("device_name", String, nullable=False),
    Column("operation", String, nullable=False),
    Column("timestamp", Float, nullable=False),
    Column("details", Text),
)

registry_metadata = Table(
    "registry_metadata",
    metadata,
    Column("key", String, primary_key=True),
    Column("value", Text),
)

standalone_pvs = Table(
    "standalone_pvs",
    metadata,
    Column("pv_name", String, primary_key=True),
    Column("description", Text),
    Column("protocol", String, nullable=False, server_default="ca"),
    Column("access_mode", String, nullable=False, server_default="read-only"),
    Column("labels", Text, nullable=False, server_default="[]"),
    Column("source", String, nullable=False, server_default="runtime"),
    Column("created_by", String),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)


def make_engine(database_url: str) -> Engine:
    """Create the shared SQLAlchemy engine for the configuration_service stores.

    ``pool_pre_ping`` recycles connections dropped by the server/proxy; the
    sync ``QueuePool`` lets the (synchronous) stores be called directly from
    FastAPI's async endpoints via the threadpool, as before.
    """
    if not database_url:
        raise ValueError(
            "database_url is empty. Set CONFIG_DATABASE_URL to a PostgreSQL DSN, e.g. "
            "postgresql+psycopg://user:pass@host:5432/config_service"
        )
    return create_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        future=True,
    )
