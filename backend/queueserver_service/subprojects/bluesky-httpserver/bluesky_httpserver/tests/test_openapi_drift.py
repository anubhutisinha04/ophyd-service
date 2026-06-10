"""
OpenAPI drift test.

Pins the two committed queueserver schema artifacts —
``subprojects/bluesky-httpserver/openapi.json`` and
``shared-schema/queueserver_service.openapi.json`` — against the schema the
export script generates from the live routers. If either diverges, regenerate
both:

    python subprojects/bluesky-httpserver/scripts/export_openapi.py
    python subprojects/bluesky-httpserver/scripts/export_openapi.py \
        -o ../../shared-schema/queueserver_service.openapi.json

``info.version`` is excluded from the comparison: it is the versioneer-derived
package version, which changes on every commit (and degrades to a placeholder
on shallow checkouts), so it is build metadata rather than API surface.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

# parents[0]=tests, [1]=bluesky_httpserver, [2]=bluesky-httpserver,
# [3]=subprojects, [4]=queueserver_service, [5]=backend, [6]=repo root.
_HTTPSERVER_ROOT = Path(__file__).resolve().parents[2]
_REPO_ROOT = Path(__file__).resolve().parents[6]

_EXPORT_SCRIPT = _HTTPSERVER_ROOT / "scripts" / "export_openapi.py"
_SUBPROJECT_SCHEMA = _HTTPSERVER_ROOT / "openapi.json"
_SHARED_SCHEMA = _REPO_ROOT / "shared-schema" / "queueserver_service.openapi.json"


def _without_version(schema: dict) -> dict:
    schema = json.loads(json.dumps(schema))
    schema.get("info", {}).pop("version", None)
    return schema


def _build_live_schema() -> dict:
    # The scripts/ directory is not a package; load the export module by path
    # so the test exercises the exact build_schema() the regeneration
    # commands use.
    spec = importlib.util.spec_from_file_location("qs_export_openapi", _EXPORT_SCRIPT)
    assert spec is not None and spec.loader is not None, _EXPORT_SCRIPT
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_schema()


def test_committed_openapi_matches_live_schema():
    live = _without_version(_build_live_schema())
    shared = _without_version(json.loads(_SHARED_SCHEMA.read_text()))
    subproject = _without_version(json.loads(_SUBPROJECT_SCHEMA.read_text()))

    assert shared == live, (
        f"OpenAPI schema drift: {_SHARED_SCHEMA} no longer matches the live "
        "routers. Regenerate it with scripts/export_openapi.py -o "
        "shared-schema/queueserver_service.openapi.json (see module docstring)."
    )
    assert subproject == live, (
        f"OpenAPI schema drift: {_SUBPROJECT_SCHEMA} no longer matches the "
        "live routers. Regenerate it with scripts/export_openapi.py "
        "(see module docstring)."
    )
