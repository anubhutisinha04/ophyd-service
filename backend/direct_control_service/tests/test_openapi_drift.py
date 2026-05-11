"""
OpenAPI drift test.

Pins ``shared-schema/direct_control.openapi.json`` against the live app's
generated schema. If they diverge, the test fails — regenerate the
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
    Path(__file__).resolve().parents[3] / "shared-schema" / "direct_control.openapi.json"
)


def test_committed_openapi_matches_app():
    from direct_control.main import app

    # Roundtrip both through JSON so the comparison is on the same shape
    # the export path produces.
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
