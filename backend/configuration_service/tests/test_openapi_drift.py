"""
OpenAPI drift test.

Pins ``shared-schema/configuration_service.openapi.json`` against the live
app's generated schema. If they diverge, the test fails — regenerate the
committed file by booting the service with
``OPHYD_SERVICE_OPENAPI_EXPORT_PATH`` set, or by running compose
(`integration/pods/*/docker-compose.yaml` already wires this up).
"""

from __future__ import annotations

import json
from pathlib import Path


# repo root from this file's directory: tests → service → backend → repo,
# i.e. ../../../ — which matches parents[3] (parents[0] is the tests dir).
_COMMITTED_SCHEMA = (
    Path(__file__).resolve().parents[3]
    / "shared-schema"
    / "configuration_service.openapi.json"
)


def test_committed_openapi_matches_app(tmp_path):
    """Build the app without entering TestClient so lifespan doesn't run.

    The schema doesn't need lifespan to be generated, and avoiding it
    sidesteps ``_maybe_export_openapi`` writing to the shared-schema
    path if ``OPHYD_SERVICE_OPENAPI_EXPORT_PATH`` happens to be set
    in the developer/CI environment.
    """
    from configuration_service.config import Settings
    from configuration_service.main import create_app

    app = create_app(Settings(use_mock_data=True, db_path=tmp_path / "t.db"))
    live = json.loads(json.dumps(app.openapi()))
    committed = json.loads(_COMMITTED_SCHEMA.read_text())

    assert live == committed, (
        "OpenAPI schema drift detected between the live app and "
        f"{_COMMITTED_SCHEMA.name}. Regenerate the committed schema "
        "by booting the service with OPHYD_SERVICE_OPENAPI_EXPORT_PATH "
        f"pointing at {_COMMITTED_SCHEMA}, or run docker compose up "
        "(any pod under integration/pods/) which writes both schemas "
        "via the export sidecar."
    )
