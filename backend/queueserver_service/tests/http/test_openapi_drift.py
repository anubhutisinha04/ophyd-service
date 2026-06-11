"""
OpenAPI drift test.

Pins the committed queueserver schema artifact —
``shared-schema/queueserver_service.openapi.json`` — against the schema the
export script generates from the live routers. If it diverges, regenerate it:

    python scripts/export_openapi.py

``info.version`` is excluded from the comparison: it is the package version,
which is build metadata rather than API surface.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

# parents[0]=http, [1]=tests, [2]=queueserver_service (service root),
# [3]=backend, [4]=repo root.
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
_REPO_ROOT = Path(__file__).resolve().parents[4]

_EXPORT_SCRIPT = _SERVICE_ROOT / "scripts" / "export_openapi.py"
_SHARED_SCHEMA = _REPO_ROOT / "shared-schema" / "queueserver_service.openapi.json"


def _without_version(schema: dict) -> dict:
    schema = json.loads(json.dumps(schema))
    schema.get("info", {}).pop("version", None)
    return schema


def _build_live_schema() -> dict:
    # The scripts/ directory is not a package; load the export module by path
    # so the test exercises the exact build_schema() the regeneration
    # command uses.
    spec = importlib.util.spec_from_file_location("qs_export_openapi", _EXPORT_SCRIPT)
    assert spec is not None and spec.loader is not None, _EXPORT_SCRIPT
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_schema()


def test_committed_openapi_matches_live_schema():
    live = _without_version(_build_live_schema())
    shared = _without_version(json.loads(_SHARED_SCHEMA.read_text()))

    assert shared == live, (
        f"OpenAPI schema drift: {_SHARED_SCHEMA} no longer matches the live "
        "routers. Regenerate it with scripts/export_openapi.py "
        "(see module docstring)."
    )
