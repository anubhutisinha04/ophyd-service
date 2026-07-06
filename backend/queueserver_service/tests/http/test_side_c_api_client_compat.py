"""Side-C compatibility suite: the ``bluesky-queueserver-api`` client over HTTP.

``queueserver_service`` is maintained independently of upstream bluesky-queueserver,
but the REST + WebSocket surface consumed by the ``bluesky-queueserver-api`` client
library (HTTP transport) is a frozen public contract. These tests drive a real
manager + HTTP server with the *actual* PyPI client (``REManagerAPI`` from
``bluesky_queueserver_api.http``) so that any drift in request/response shapes,
auth handling, or error semantics fails CI.

This complements:

* side-A (configuration_service <-> direct_control_service) and side-B
  (configuration_service <-> queueserver_service) edge suites, and
* the containerized ``integration/exercise/queueserver_api_compat.py`` exerciser,
  which drives the client over BOTH 0MQ and HTTP against a running pod.

Because the 0MQ transport is slated for removal (the service is becoming
HTTP-only), this in-process HTTP suite is the acceptance gate that the removal
must not regress. It uses the single-user API-key auth mode (the default the HTTP
transport is normally driven with); token/session client auth is covered
separately in ``test_side_c_auth.py``.
"""

import time as ttime

import pytest
from tests.manager.common import re_manager_cmd  # noqa: F401

from tests.http.conftest import (  # noqa: F401
    API_KEY_FOR_TESTS,
    SERVER_ADDRESS,
    SERVER_PORT,
    fastapi_server,
)

# The real client library under test.
from bluesky_queueserver_api import BFunc, BPlan
from bluesky_queueserver_api.http import REManagerAPI

_HTTP_URI = f"http://{SERVER_ADDRESS}:{SERVER_PORT}"
_WAIT = 30  # generous per-call wait ceiling; loaded CI runners are slow


@pytest.fixture
def re_manager_console(re_manager_cmd):  # noqa: F811
    """A fresh RE Manager per test, with 0MQ console publishing enabled so the
    HTTP console monitor has something to stream (harmless for the other tests)."""
    re_manager_cmd(["--zmq-publish-console=ON"])
    yield


@pytest.fixture
def rm(fastapi_server):  # noqa: F811
    """A ``bluesky_queueserver_api.http.REManagerAPI`` bound to the test server and
    authenticated with the single-user API key."""
    client = REManagerAPI(http_server_uri=_HTTP_URI)
    client.set_authorization_key(api_key=API_KEY_FOR_TESTS)
    yield client
    client.close()


def _wait_for_manager_state(rm, states, timeout=_WAIT, period=0.2):
    deadline = ttime.monotonic() + timeout
    while ttime.monotonic() < deadline:
        if rm.status()["manager_state"] in states:
            return True
        ttime.sleep(period)
    return False


def test_side_c_methods_without_environment(re_manager_console, rm):
    """Every read-only + queue-editing + permissions + locks client method
    round-trips over HTTP with the documented response shape, plus error
    semantics (failed request and bad credentials)."""

    # --- status / ping / discovery -----------------------------------------
    status = rm.status()
    assert status["manager_state"] == "idle", status
    assert status["worker_environment_exists"] is False, status
    assert status["items_in_queue"] == 0, status

    assert "manager_state" in rm.ping(), "ping should return the status document"

    config = rm.config_get()
    assert config["success"] is True and "config" in config, config

    scopes = rm.api_scopes()
    assert "scopes" in scopes, scopes

    # --- allowed / existing plans & devices ---------------------------------
    plans_allowed = rm.plans_allowed()
    assert plans_allowed["success"] and "count" in plans_allowed["plans_allowed"], plans_allowed
    devices_allowed = rm.devices_allowed()
    assert devices_allowed["success"] and "det1" in devices_allowed["devices_allowed"], devices_allowed
    assert rm.plans_existing()["success"], "plans_existing failed"
    assert rm.devices_existing()["success"], "devices_existing failed"

    # --- permissions get / set (round-trip) / reload ------------------------
    perms = rm.permissions_get()
    assert perms["success"] and "user_group_permissions" in perms, perms
    assert rm.permissions_set(user_group_permissions=perms["user_group_permissions"])["success"]
    assert rm.permissions_reload()["success"]

    # --- queue editing ------------------------------------------------------
    assert rm.queue_clear()["success"]
    added = rm.item_add(BPlan("count", ["det1"], num=1))
    assert added["success"] and added["qsize"] == 1 and "item_uid" in added["item"], added

    batch = rm.item_add_batch(items=[BPlan("count", ["det1"]), BPlan("scan", ["det1"], "motor", -1, 1, 3)])
    assert batch["success"] and len(batch["items"]) == 2, batch

    queue = rm.queue_get()
    assert queue["success"] and len(queue["items"]) == 3, queue
    assert "running_item" in queue and "plan_queue_uid" in queue, queue

    first_item = queue["items"][0]
    updated = rm.item_update(item={**first_item, "kwargs": {"num": 5}})
    assert updated["success"], updated
    assert rm.item_move(pos=0, pos_dest=1)["success"], "item_move failed"
    assert rm.item_get(pos=0)["success"], "item_get failed"
    removed = rm.item_remove(pos=0)
    assert removed["success"] and removed["qsize"] == 2, removed
    assert rm.queue_clear()["success"]
    assert len(rm.queue_get()["items"]) == 0, "queue should be empty after clear"

    # --- history ------------------------------------------------------------
    history = rm.history_get()
    assert history["success"] and "items" in history, history
    assert rm.history_clear()["success"]

    # --- error semantics ----------------------------------------------------
    # A rejected request surfaces as RequestFailedError (success=False -> raise).
    with pytest.raises(rm.RequestFailedError):
        rm.item_add(BPlan("this_plan_does_not_exist"))

    # An admin-only endpoint is refused for the single (non-admin) user.
    with pytest.raises(rm.HTTPClientError):
        rm.principal_info()

    # Wrong API key -> HTTP 401.
    bad = REManagerAPI(http_server_uri=_HTTP_URI)
    bad.set_authorization_key(api_key="THIS-IS-NOT-THE-KEY")
    try:
        with pytest.raises(bad.HTTPClientError):
            bad.status()
    finally:
        bad.close()

    # --- locks (lock everything, read lock info, unlock) --------------------
    lock_key = "side-c-lock-key"
    assert rm.lock_all(lock_key=lock_key)["success"], "lock_all failed"
    lock_info = rm.lock_info()
    assert lock_info["success"] and lock_info["lock_info"]["environment"], lock_info
    assert rm.unlock(lock_key=lock_key)["success"], "unlock failed"
    assert rm.lock_info()["lock_info"]["environment"] is False, "environment should be unlocked"


def test_side_c_environment_plan_tasks_and_console(re_manager_console, rm):
    """Environment lifecycle, plan execution + history, task submission
    (script_upload / function_execute) with task_status/task_result, and the HTTP
    console monitor."""

    assert rm.environment_open()["success"], "environment_open failed"
    rm.wait_for_idle(timeout=_WAIT)  # raises WaitTimeoutError if the env never opens
    assert rm.status()["worker_environment_exists"] is True

    rm.queue_clear()

    # Console monitor over HTTP: enable before the plan runs so its output streams.
    rm.console_monitor.enable()

    added = rm.item_add(BPlan("count", ["det1"], num=3, delay=0.2))
    assert added["success"], added
    assert rm.queue_start()["success"], "queue_start failed"
    rm.wait_for_idle(timeout=_WAIT)

    history = rm.history_get()
    assert len(history["items"]) == 1, history
    assert history["items"][-1]["result"]["exit_status"] == "completed", history["items"][-1]

    # The console monitor should have seen at least one message from the run.
    console_msgs = []
    deadline = ttime.monotonic() + _WAIT
    while ttime.monotonic() < deadline and not console_msgs:
        try:
            msg = rm.console_monitor.next_msg(timeout=2)
            if msg:
                console_msgs.append(msg)
        except rm.RequestTimeoutError:
            continue
    rm.console_monitor.disable()
    assert console_msgs, "console monitor received no messages over HTTP"
    assert "msg" in console_msgs[0] and "time" in console_msgs[0], console_msgs[0]

    # script_upload -> a task; poll it to completion and read its result.
    uploaded = rm.script_upload("side_c_probe_value = 12345\n")
    assert uploaded["success"] and uploaded["task_uid"], uploaded
    task_uid = uploaded["task_uid"]
    rm.wait_for_completed_task(task_uid, timeout=_WAIT)
    task_status = rm.task_status(task_uid)
    assert task_status["success"] and task_status["status"] == "completed", task_status
    task_result = rm.task_result(task_uid)
    assert task_result["success"] and task_result["result"]["success"], task_result

    # function_execute -> a task as well (the sim profile exposes 'function_sleep').
    executed = rm.function_execute(BFunc("function_sleep", 0.2))
    assert executed["success"] and executed["task_uid"], executed
    rm.wait_for_completed_task(executed["task_uid"], timeout=_WAIT)
    assert rm.task_result(executed["task_uid"])["success"]

    assert rm.environment_close()["success"], "environment_close failed"
    rm.wait_for_idle(timeout=_WAIT)
    assert rm.status()["worker_environment_exists"] is False


def test_side_c_run_engine_control(re_manager_console, rm):
    """Run Engine control over HTTP: pause/resume a running plan, then
    pause/stop it."""

    assert rm.environment_open()["success"]
    rm.wait_for_idle(timeout=_WAIT)
    rm.queue_clear()

    # A plan long enough to catch mid-flight.
    rm.item_add(BPlan("count", ["det1"], num=5, delay=1))
    assert rm.queue_start()["success"]
    assert _wait_for_manager_state(rm, ("executing_queue",)), "queue did not start executing"

    assert rm.re_pause(option="deferred")["success"], "re_pause failed"
    rm.wait_for_idle_or_paused(timeout=_WAIT)
    assert rm.status()["re_state"] == "paused", rm.status()

    assert rm.re_resume()["success"], "re_resume failed"
    rm.wait_for_idle(timeout=_WAIT)

    # Now exercise the stop path: pause again, then stop the run.
    rm.item_add(BPlan("count", ["det1"], num=5, delay=1))
    assert rm.queue_start()["success"]
    assert _wait_for_manager_state(rm, ("executing_queue",)), "queue did not start executing (2)"
    assert rm.re_pause(option="deferred")["success"]
    rm.wait_for_idle_or_paused(timeout=_WAIT)
    assert rm.status()["re_state"] == "paused"
    assert rm.re_stop()["success"], "re_stop failed"
    rm.wait_for_idle(timeout=_WAIT)

    rm.queue_clear()
    assert rm.environment_close()["success"]
    rm.wait_for_idle(timeout=_WAIT)


@pytest.mark.xfail(
    reason="GET /api/auth/whoami currently returns HTTP 500 (the single-user "
    "principal is never resolved); tracked as a separate auth fix. This test "
    "flips to xpass once whoami returns the principal.",
    strict=False,
)
def test_side_c_whoami_returns_principal(rm):
    """The client's ``whoami()`` should return the current principal document."""
    principal = rm.whoami()
    assert isinstance(principal, dict) and principal.get("success", True)
