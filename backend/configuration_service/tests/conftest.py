"""
Pytest fixtures for Configuration Service tests.

All tests use mock data — no external profile collections required. The suite is
parametrized over both supported database backends, so every DB-touching test
runs once against SQLite and once against PostgreSQL:

- **SQLite** needs nothing: each test gets a fresh temp-file database.
- **PostgreSQL** needs a reachable instance. Point TEST_DATABASE_URL at one, e.g.

      docker run --rm -d -p 5432:5432 \
          -e POSTGRES_USER=bluesky -e POSTGRES_PASSWORD=bluesky -e POSTGRES_DB=config_service \
          postgres:16
      export TEST_DATABASE_URL=postgresql+psycopg://bluesky:bluesky@localhost:5432/config_service

  If TEST_DATABASE_URL is unset and the default localhost instance is
  unreachable, the PostgreSQL parameter is skipped (so local dev can run the
  SQLite half without Docker). CI provides a `postgres` service container, so
  both halves run there.

Each test gets a clean schema: the fixture drops and recreates all tables before
the app starts, so the lifespan re-seeds from mock data every time.
"""

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from configuration_service.config import Settings
from configuration_service.db import make_engine, metadata
from configuration_service.main import create_app

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://bluesky:bluesky@localhost:5432/config_service",
)

# Set when TEST_DATABASE_URL is explicitly provided: in that case an unreachable
# PostgreSQL is a hard failure rather than a silent skip, so a misconfigured CI
# doesn't quietly run only the SQLite half.
_PG_REQUIRED = "TEST_DATABASE_URL" in os.environ


def _reset_schema(url: str) -> None:
    engine = make_engine(url)
    try:
        metadata.drop_all(engine)
        metadata.create_all(engine)
    finally:
        engine.dispose()


@pytest.fixture(params=["sqlite", "postgresql"])
def db_url(request, tmp_path) -> str:
    """A clean test database for the parametrized backend; returns its DSN.

    Use in ``Settings(database_url=db_url)``. Each test starts from an empty
    schema; the app lifespan re-seeds from mock data on startup.
    """
    if request.param == "sqlite":
        url = f"sqlite+pysqlite:///{tmp_path / 'config_test.db'}"
        _reset_schema(url)
        return url

    # postgresql
    try:
        _reset_schema(TEST_DATABASE_URL)
    except OperationalError as exc:
        if _PG_REQUIRED:
            raise
        pytest.skip(f"PostgreSQL not reachable at TEST_DATABASE_URL: {exc}")
    return TEST_DATABASE_URL


@pytest.fixture
def db_engine(db_url):
    """An Engine on the clean test database, for tests that drive a store directly."""
    engine = make_engine(db_url)
    yield engine
    engine.dispose()


@pytest.fixture
def mock_settings(db_url) -> Settings:
    """Settings configured for mock data against the clean test database."""
    return Settings(use_mock_data=True, database_url=db_url)


@pytest.fixture
def mock_client(mock_settings) -> TestClient:
    """Test client with mock data."""
    app = create_app(mock_settings)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def client(mock_settings) -> TestClient:
    """Default test client (mock data)."""
    app = create_app(mock_settings)
    with TestClient(app) as client:
        yield client
