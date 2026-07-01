"""HTTP-level tests for the fork's custom device-registry endpoints.

These exercise the ``/api/devices/diff_against_profile`` and
``/api/devices/sync_from_profile`` routes through the real FastAPI server +
RE Manager (the config-service integration is *disabled* here — the default
"standalone" configuration), so they cover the pieces the manager-level
``test_config_service*`` suites do not:

- the routes are wired into the app and reachable,
- the ``success: False`` manager envelope is mapped to an HTTP status,
- the auth scopes on the routes are enforced,
- the router's own request validation (unknown ``strategy``) rejects before
  anything is dispatched to the manager.

With config-service disabled the manager handlers short-circuit to a
"feature is disabled" envelope, which is exactly the standalone contract:
the endpoints exist and answer, and they answer "disabled" rather than
erroring or 404ing.
"""

import pytest
import requests
from fastapi import HTTPException
from tests.manager.common import (  # noqa: F401
    re_manager,
    re_manager_factory,
)

from queueserver_service.http.routers.profile_collection import (
    _raise_device_command_failure,
)
from queueserver_service.manager.config_service import (
    ERROR_KIND_CONFIG_SERVICE_UNREACHABLE,
)
from tests.http.conftest import (  # noqa: F401
    API_KEY_FOR_TESTS,
    SERVER_ADDRESS,
    SERVER_PORT,
    fastapi_server_fs,
)


def _url(path):
    return f"http://{SERVER_ADDRESS}:{SERVER_PORT}/api{path}"


def _auth_headers(api_key=API_KEY_FOR_TESTS):
    return {"Authorization": f"ApiKey {api_key}"} if api_key else {}


def test_devices_diff_against_profile_disabled(re_manager, fastapi_server_fs):  # noqa: F811
    """GET diff endpoint answers 409 "disabled" when config-service is off."""
    fastapi_server_fs()

    resp = requests.get(_url("/devices/diff_against_profile"), headers=_auth_headers())
    assert resp.status_code == 409, resp.text
    assert "disabled" in resp.json().get("detail", "").lower(), resp.text


def test_devices_sync_from_profile_disabled(re_manager, fastapi_server_fs):  # noqa: F811
    """POST sync endpoint answers 409 "disabled" when config-service is off."""
    fastapi_server_fs()

    resp = requests.post(
        _url("/devices/sync_from_profile"),
        headers=_auth_headers(),
        json={"strategy": "all"},
    )
    assert resp.status_code == 409, resp.text
    assert "disabled" in resp.json().get("detail", "").lower(), resp.text


def test_devices_sync_rejects_unknown_strategy(re_manager, fastapi_server_fs):  # noqa: F811
    """The router validates ``strategy`` before dispatching to the manager."""
    fastapi_server_fs()

    resp = requests.post(
        _url("/devices/sync_from_profile"),
        headers=_auth_headers(),
        json={"strategy": "definitely-not-a-strategy"},
    )
    assert resp.status_code == 409, resp.text
    assert "strategy" in resp.json().get("detail", "").lower(), resp.text


def test_devices_endpoints_require_auth(re_manager, fastapi_server_fs):  # noqa: F811
    """Both endpoints reject unauthenticated requests (scopes are enforced)."""
    fastapi_server_fs()

    r_get = requests.get(_url("/devices/diff_against_profile"), headers=_auth_headers(api_key=None))
    assert r_get.status_code in (401, 403), r_get.text

    r_post = requests.post(
        _url("/devices/sync_from_profile"),
        headers=_auth_headers(api_key=None),
        json={"strategy": "all"},
    )
    assert r_post.status_code in (401, 403), r_post.text


# --- status-mapping unit tests (fast; no server) ---------------------------
#
# The router maps a failed manager config-service envelope to an HTTP status:
# an outage (error_kind == config_service_unreachable) -> 503; everything else
# (disabled, no environment, conflict, malformed) -> 409.


def test_status_mapping_unreachable_is_503():
    with pytest.raises(HTTPException) as exc_info:
        _raise_device_command_failure(
            {"error_kind": ERROR_KIND_CONFIG_SERVICE_UNREACHABLE, "msg": "cfg down"},
            default_detail="diff failed",
        )
    assert exc_info.value.status_code == 503
    assert "cfg down" in exc_info.value.detail


def test_status_mapping_other_failure_is_409():
    with pytest.raises(HTTPException) as exc_info:
        _raise_device_command_failure(
            {"msg": "configuration-service feature is disabled on this manager"},
            default_detail="diff failed",
        )
    assert exc_info.value.status_code == 409
    assert "disabled" in exc_info.value.detail


def test_status_mapping_falls_back_to_default_detail():
    with pytest.raises(HTTPException) as exc_info:
        _raise_device_command_failure({}, default_detail="diff failed")
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "diff failed"
