"""
Persistence layer for configuration_service.

Defines the shared SQLAlchemy Core schema (one MetaData for all tables) and the
engine factory. ``device_registry_store`` and ``standalone_pv_store`` build their
queries against these Table objects; ``main.py`` owns the single Engine (created
once at startup) and injects it into both stores.

Two backends are supported, selected by the DSN scheme passed to ``make_engine``:
- PostgreSQL (``postgresql+psycopg://...``) for production / multi-writer deploys.
- SQLite (``sqlite+pysqlite:///path``) for single-node / dev use.

The schema is deliberately portable — only ``String``/``Text``/``Float``/
``BigInteger`` types, no JSONB/ARRAY — so the same definitions create cleanly on
either backend. The only dialect-specific touch points are the upsert (see
``upsert``) and the audit-id column (see below); both are handled here so the
stores stay backend-agnostic.

Schema notes:
- Timestamps are epoch floats (``Float`` -> DOUBLE PRECISION on PG, REAL on
  SQLite) to preserve the existing wire/API shape.
- ``device_audit_log.id`` is a monotonic, gap-tolerant sequence — the change-feed
  cursor bluesky-queueserver Layer 2 polls via
  ``GET /api/v1/devices/changes?since_version=N``. Never reuse or reset it.
  On PostgreSQL it is a BIGINT IDENTITY column; ``.with_variant(Integer,
  "sqlite")`` renders it as ``INTEGER PRIMARY KEY`` on SQLite, which is the only
  form SQLite treats as an auto-incrementing rowid alias — a bare BIGINT PK does
  NOT autoincrement and fails the NOT NULL constraint on insert. BIGINT on PG
  (not INT) because the id grows on every seed/CRUD/reset/lock event (lock events
  log one row per affected device), so a 32-bit range could be exhausted.
- ``standalone_pvs.labels`` is a JSON-encoded TEXT column (parsed in Python) to
  keep the existing wire/API shape and stay portable across both backends.
"""

from typing import Union

from sqlalchemy import (
    BigInteger,
    Column,
    Float,
    Identity,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    event,
)
from sqlalchemy.engine import Connection, Engine, make_url

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
    Column(
        "id",
        BigInteger().with_variant(Integer, "sqlite"),
        Identity(),
        primary_key=True,
    ),
    Column("device_name", String, nullable=False),
    Column("operation", String, nullable=False),
    Column("timestamp", Float, nullable=False),
    Column("details", Text),
    # AUTOINCREMENT on SQLite so deleted ids are never reused — matching
    # PostgreSQL IDENTITY. Without it, SQLite's rowid is max(id)+1 and would
    # reuse the id of a deleted top row, breaking the never-reused guarantee the
    # /changes cursor depends on. No-op on PostgreSQL.
    sqlite_autoincrement=True,
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


def upsert(bind: Union[Engine, Connection], table: Table):
    """Return a dialect-appropriate INSERT construct supporting ON CONFLICT.

    Both PostgreSQL and SQLite (3.24+) implement ``INSERT ... ON CONFLICT DO
    UPDATE`` with the same SQLAlchemy surface — ``.on_conflict_do_update(
    index_elements=..., set_=...)`` — but the construct lives in the dialect
    package, so the right ``insert`` has to be picked per backend. Callers use
    the returned object exactly as they would the dialect insert.
    """
    if bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    elif bind.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    else:
        raise ValueError(
            f"Unsupported database backend '{bind.dialect.name}'. "
            "configuration_service supports postgresql and sqlite."
        )
    return dialect_insert(table)


def make_engine(database_url: str) -> Engine:
    """Create the shared SQLAlchemy engine for the configuration_service stores.

    Accepts either a PostgreSQL DSN (``postgresql+psycopg://...``) or a SQLite
    DSN (``sqlite+pysqlite:///path``); the backend is inferred from the scheme.

    For PostgreSQL: ``pool_pre_ping`` recycles connections dropped by the
    server/proxy, and a ``QueuePool`` lets the synchronous stores be called
    directly from FastAPI's async endpoints via the threadpool.

    For SQLite: ``check_same_thread=False`` is required because that same
    threadpool hands connections across threads, and ``timeout`` sets the
    busy-timeout so concurrent writers wait for the single writer lock instead
    of erroring immediately. WAL journaling is enabled per-connection so readers
    don't block behind a mid-commit writer. (SQLite is intended for
    single-node/dev use; for multi-writer production, use PostgreSQL.)
    """
    if not database_url:
        raise ValueError(
            "database_url is empty. Set CONFIG_DATABASE_URL to a PostgreSQL DSN "
            "(postgresql+psycopg://user:pass@host:5432/config_service) or a SQLite "
            "DSN (sqlite+pysqlite:////var/lib/config_service/config.db)."
        )

    backend = make_url(database_url).get_backend_name()
    if backend == "sqlite":
        engine = create_engine(
            database_url,
            connect_args={"check_same_thread": False, "timeout": 30},
            future=True,
        )

        @event.listens_for(engine, "connect")
        def _enable_wal(dbapi_conn, _record):
            # WAL lets reads proceed concurrently with a writer (a no-op on
            # in-memory databases, which are always journal-mode "memory").
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        return engine
    if backend == "postgresql":
        return create_engine(
            database_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
            future=True,
        )
    raise ValueError(
        f"Unsupported database backend '{backend}' in CONFIG_DATABASE_URL. "
        "configuration_service supports postgresql and sqlite."
    )
