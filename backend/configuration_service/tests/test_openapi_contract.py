"""
Schemathesis-driven OpenAPI contract test.

Property-based fuzz of every documented GET endpoint. The minimum bar is
``not_a_server_error``: no schema-compliant *or* schema-violating input
should surface as a 5xx.

Scope notes:
- GET-only for now. Mutating endpoints (POST/PUT/DELETE) accumulate state
  in the module-scoped app+db across hypothesis examples, producing
  invalid-state 5xxs that mask real findings. Per-call DB reset is the
  right fix for full coverage; tracked separately.
- Stricter checks (status-code conformance, response-schema conformance,
  positive/negative data acceptance) can be turned on per-endpoint as
  known divergences from the FastAPI-generated schema are resolved.
  FastAPI describes ``Optional[Enum]`` query params as accepting the
  literal string ``"null"``, which the enum validator rejects with 422 —
  a global divergence, not a bug worth chasing inside this test.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import schemathesis
from hypothesis import settings as hypothesis_settings

from configuration_service.config import Settings
from configuration_service.main import create_app


# Module-scoped app so `@schema.parametrize()` can decorate at import time.
# Mock-data mode + tmp sqlite keeps the registry isolated from other tests.
_db_path = Path(tempfile.mkdtemp(prefix="schemathesis-")) / "test.db"
_settings = Settings(use_mock_data=True, db_path=_db_path)
_app = create_app(_settings)

schema = schemathesis.openapi.from_asgi("/openapi.json", _app).include(method="GET")


@schema.parametrize()
@hypothesis_settings(max_examples=10, deadline=None)
def test_openapi_no_server_errors(case):
    case.call_and_validate(checks=(schemathesis.checks.not_a_server_error,))
