"""Contract guard: config's direct-control enrich client vs dc's published schema.

configuration_service calls direct_control's ``POST /api/v1/devices/enrich`` via
``DirectControlClient``. The request/response shapes are hand-mirrored in
``direct_control_client.py`` (``EnrichmentSpec`` / ``EnrichmentResult``), so a
change to direct_control's published contract
(``shared-schema/direct_control.openapi.json``) could silently break enrichment.

This test pins config's client models to that contract: a drift on either side
fails CI here rather than surfacing at runtime as every deferred slot degrading
to ``ENRICHMENT_UNAVAILABLE``.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from configuration_service.direct_control_client import (
    EnrichmentResult,
    EnrichmentSpec,
)

# tests[0] -> configuration_service[1] -> backend[2] -> repo root[3]
_CONTRACT = Path(__file__).resolve().parents[3] / "shared-schema" / "direct_control.openapi.json"
_ENRICH_PATH = "/api/v1/devices/enrich"


def _schema() -> dict:
    return json.loads(_CONTRACT.read_text(encoding="utf-8"))


def _resolve(schema: dict, node: dict) -> dict:
    """Resolve a possibly-``$ref`` node to its component schema."""
    ref = node.get("$ref")
    if not ref:
        return node
    assert ref.startswith("#/"), ref
    target: dict = schema
    for part in ref[2:].split("/"):
        target = target[part]
    return target


def _enrich_post(schema: dict) -> dict:
    paths = schema["paths"]
    assert _ENRICH_PATH in paths, f"{_ENRICH_PATH} missing from {_CONTRACT}"
    return paths[_ENRICH_PATH]["post"]


def test_enrich_request_items_match_client_spec_fields():
    schema = _schema()
    op = _enrich_post(schema)
    req = _resolve(schema, op["requestBody"]["content"]["application/json"]["schema"])
    assert "items" in req["properties"], "enrich request must carry an 'items' array"
    assert "items" in req.get("required", []), "'items' must be required"

    spec = _resolve(schema, req["properties"]["items"]["items"])
    client_fields = {f.name for f in dataclasses.fields(EnrichmentSpec)}
    contract_fields = set(spec["properties"])
    assert client_fields == contract_fields, (
        "config's EnrichmentSpec fields drifted from direct_control's contract: "
        f"client={sorted(client_fields)} contract={sorted(contract_fields)}"
    )
    # config sends every field and the contract forbids extras, so all are required.
    assert set(spec.get("required", [])) == client_fields, (
        "every enrich spec field must be required by the contract: "
        f"required={sorted(spec.get('required', []))} client={sorted(client_fields)}"
    )


def test_enrich_response_results_match_client_result_fields():
    schema = _schema()
    op = _enrich_post(schema)
    resp = _resolve(schema, op["responses"]["200"]["content"]["application/json"]["schema"])
    assert "results" in resp["properties"], "enrich response must carry a 'results' array"

    item = _resolve(schema, resp["properties"]["results"]["items"])
    client_fields = {f.name for f in dataclasses.fields(EnrichmentResult)}
    contract_fields = set(item["properties"])
    assert client_fields == contract_fields, (
        "config's EnrichmentResult fields drifted from direct_control's contract: "
        f"client={sorted(client_fields)} contract={sorted(contract_fields)}"
    )
    # config reads row["ok"] unconditionally, so the contract must mark it required.
    assert "ok" in item.get("required", []), (
        "'ok' must be a required field of the enrich result (client reads it unconditionally)"
    )
