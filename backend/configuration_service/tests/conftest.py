"""
Pytest fixtures for Configuration Service tests.

All tests use mock data — no external profile collections required.
"""

import pytest
from fastapi.testclient import TestClient

from configuration_service.main import create_app
from configuration_service.config import Settings


@pytest.fixture
def mock_settings(tmp_path) -> Settings:
    """Settings configured for mock data."""
    return Settings(use_mock_data=True, db_path=tmp_path / "test.db")


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
