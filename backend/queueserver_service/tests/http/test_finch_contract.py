"""finch frontend contract guard for the queueserver HTTP API.

The finch frontend drives the queueserver through its qServer client
(``src/api/qServer/requests.ts``), talking to a base URL that ends in ``/api``.
This test pins the endpoints finch depends on: each must stay present in the
committed OpenAPI contract (``shared-schema/queueserver_service.openapi.json``),
so a backend change that drops or renames one fails CI here instead of silently
breaking the frontend.

If finch adds or removes a call in ``requests.ts``, update
``FINCH_QSERVER_ENDPOINTS`` to match — it is the source of truth for what the
frontend consumes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# parents[0]=http, [1]=tests, [2]=queueserver_service, [3]=backend, [4]=repo root
_REPO_ROOT = Path(__file__).resolve().parents[4]
_SHARED_SCHEMA = _REPO_ROOT / "shared-schema" / "queueserver_service.openapi.json"

# (HTTP method, OpenAPI path) pairs the finch qServer client calls. finch uses a
# base URL ending in ``/api`` and calls the paths without that prefix, so the
# contract paths below carry the ``/api`` prefix; path parameters use the
# contract's templated form.
# Source of truth: finch src/api/qServer/requests.ts
FINCH_QSERVER_ENDPOINTS = [
    ("get", "/api/queue/get"),
    ("get", "/api/history/get"),
    ("get", "/api/status"),
    ("get", "/api/plans/allowed"),
    ("get", "/api/devices/allowed"),
    ("get", "/api/queue/item/{item_uid}"),
    ("post", "/api/environment/open"),
    ("post", "/api/queue/item/add"),
    ("post", "/api/queue/item/execute"),
    ("post", "/api/queue/item/remove"),
    ("get", "/api/re/runs/active"),
    ("post", "/api/queue/start"),
    ("post", "/api/re/pause"),
    ("post", "/api/re/resume"),
    ("post", "/api/re/abort"),
]


def _contract_paths() -> dict:
    return json.loads(_SHARED_SCHEMA.read_text(encoding="utf-8"))["paths"]


@pytest.mark.parametrize("method,path", FINCH_QSERVER_ENDPOINTS)
def test_finch_qserver_endpoint_present(method, path):
    paths = _contract_paths()
    assert path in paths, (
        f"finch calls {method.upper()} {path} but it is absent from the committed "
        f"OpenAPI contract. See finch src/api/qServer/requests.ts."
    )
    assert method in paths[path], (
        f"finch calls {method.upper()} {path} but the contract only defines "
        f"methods {sorted(paths[path])} for that path."
    )
