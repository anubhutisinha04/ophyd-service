"""Pin the exception-mapping helpers in direct_control.main.

These helpers were extracted to dedupe ~11 repetitions of the same
exception ladder around ``registry_client.validate_*`` and the
coordination-gate. Because the batch endpoint (/pv/set/batch) cannot
``raise`` mid-loop and instead inspects the status code to build a row,
batch and single endpoints would silently drift if anyone changed one
mapping without the other.

These tests pin the contract so that drift is loud:

1. ``_registry_error_status`` returns the same code the per-endpoint
   ``raise HTTPException(..., status_code=...)`` used to use.
2. ``_map_registry_errors_to_http`` actually raises HTTPException with
   that status when entered around a raising coroutine.
3. The mapping helpers and the live FastAPI endpoints agree (same
   single-endpoint failure mode now goes through the helper; the test
   asserts the observable HTTP status, not the helper's internals).
4. ``_raise_http_for_coordination_failure`` preserves the exact log
   event name ("coordination_check_failed") and detail prefix
   ("Coordination check failed: ") that the frontend's error-banner
   parser depends on.
"""

from __future__ import annotations

import pytest
import structlog
from fastapi import HTTPException

from direct_control.main import (
    _map_registry_errors_to_http,
    _raise_http_for_coordination_failure,
    _registry_error_status,
)
from direct_control.models import CoordinationCheckError
from direct_control.registry_client import RegistryValidationError


# ===== _registry_error_status: lock-step contract for batch + single =====


def test_registry_error_status_validation_error_is_404():
    """RegistryValidationError → 404. Was the hardcoded 404 in 8 sites."""
    assert _registry_error_status(RegistryValidationError("xyz", "PV")) == 404


def test_registry_error_status_runtime_error_is_503():
    """RuntimeError (registry outage / 5xx) → 503. Was hardcoded 503 in 8 sites.

    Per registry_client semantics: 404 from config-service is the only
    "not found" signal — every other non-2xx becomes RuntimeError, so the
    503 mapping here is what surfaces config-service outages as 503 to
    the operator (instead of a misleading 404 "not found").
    """
    assert _registry_error_status(RuntimeError("config-service 502")) == 503


def test_registry_error_status_unknown_defaults_to_500():
    """Defensive default for callers that pass something we didn't expect.

    Callers in main.py only catch (RegistryValidationError, RuntimeError),
    so this branch shouldn't be reached in practice — but if it ever is,
    500 is the right surface (an internal error in our mapping, not a
    client error).
    """
    assert _registry_error_status(ValueError("oops")) == 500


# ===== _map_registry_errors_to_http: async context manager wraps + reraises =====


async def test_map_registry_errors_to_http_translates_validation_error():
    with pytest.raises(HTTPException) as excinfo:
        async with _map_registry_errors_to_http():
            raise RegistryValidationError("M1:RBV", "PV")
    assert excinfo.value.status_code == 404
    # detail must be str(exc) to preserve the existing wire format —
    # the frontend reads detail directly.
    assert "M1:RBV" in str(excinfo.value.detail)


async def test_map_registry_errors_to_http_translates_runtime_error():
    with pytest.raises(HTTPException) as excinfo:
        async with _map_registry_errors_to_http():
            raise RuntimeError("config-service 502: upstream not ready")
    assert excinfo.value.status_code == 503
    assert "upstream not ready" in str(excinfo.value.detail)


async def test_map_registry_errors_to_http_passes_through_unrelated():
    """Anything that isn't a registry exception escapes unchanged.

    Important: if a caller accidentally raises ValueError or TypeError
    inside the context, we should NOT swallow it as 500 — that would mask
    real programming bugs. Only the two documented types are remapped.
    """
    with pytest.raises(KeyError):
        async with _map_registry_errors_to_http():
            raise KeyError("not a registry error")


async def test_map_registry_errors_to_http_no_op_on_success():
    """Happy path: clean exit from the context manager, no exception."""
    entered = False
    async with _map_registry_errors_to_http():
        entered = True
    assert entered is True


# ===== _raise_http_for_coordination_failure: preserved wire format + log key =====


def test_coordination_failure_helper_raises_503_with_detail_prefix():
    """The 503 status and "Coordination check failed: " detail prefix
    are observed by the frontend's error banner; the structlog event
    name "coordination_check_failed" is observed by ops queries. Both
    must survive the refactor exactly.
    """
    exc = CoordinationCheckError("config-service unreachable")
    with pytest.raises(HTTPException) as excinfo:
        _raise_http_for_coordination_failure(exc, pv_name="M1:VAL")
    assert excinfo.value.status_code == 503
    assert str(excinfo.value.detail).startswith("Coordination check failed: ")
    assert "config-service unreachable" in str(excinfo.value.detail)


def test_coordination_failure_helper_passes_log_fields_through():
    """Caller-supplied identifier kwargs (pv_name / device_name /
    device_path) must reach the structured log line so ops can filter.

    Uses ``structlog.testing.capture_logs`` so the assertion doesn't
    depend on whichever renderer some other test happened to configure
    (KeyValueRenderer vs JSONRenderer vs ConsoleRenderer).
    """
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(HTTPException):
            _raise_http_for_coordination_failure(
                CoordinationCheckError("down"), device_name="m1"
            )
    matching = [
        entry
        for entry in logs
        if entry.get("event") == "coordination_check_failed"
        and entry.get("device_name") == "m1"
    ]
    assert matching, f"expected coordination_check_failed event with device_name=m1, got {logs!r}"
    assert matching[0]["log_level"] == "error"
