"""Regression tests for manager-worker liveness and pipe robustness.

* Worker-death detection: the manager must detect a dead RE Worker process and
  recover, instead of staying stuck in EXECUTING_QUEUE / CREATING_ENVIRONMENT
  forever after a worker crash. Detection requires several consecutive
  ``request_state`` pipe timeouts AND a watchdog confirmation that the process is
  actually dead (it never kills on timeouts alone, so a slow-but-alive env-open
  is not aborted).
* Late pipe responses: a late/duplicate response arriving after ``wait_for``
  cancelled the receive future must not raise ``InvalidStateError`` in the
  detached ``_response_received`` task.

The manager is built via ``__new__`` (bypassing the multiprocessing-heavy
``__init__``); only the attributes each method under test touches are populated.
"""

from __future__ import annotations

import asyncio
import contextlib
import multiprocessing

import pytest

from queueserver_service.common.comms import CommTimeoutError, PipeJsonRpcSendAsync
from queueserver_service.manager.manager import MState, RunEngineManager


def _death_manager(*, state, environment_exists, limit=3, alive):
    """Build a bare manager wired for worker-death-detection tests.

    ``alive`` is whether the (stubbed) watchdog considers the worker alive; the
    detection path only recovers when the watchdog *positively confirms* death.
    """
    mgr = RunEngineManager.__new__(RunEngineManager)
    mgr._environment_exists = environment_exists
    mgr._manager_state = state
    mgr._worker_state_timeout_count = 0
    mgr._worker_state_timeout_limit = limit
    mgr._worker_death_handled = False
    mgr._loop = asyncio.get_running_loop()

    async def _confirm_dead():
        return not alive

    mgr._watchdog_confirm_worker_dead = _confirm_dead

    scheduled = {"kills": 0}

    async def _kill():
        scheduled["kills"] += 1

    mgr._kill_re_worker_task = _kill

    async def _exec_bg(coro):
        return await coro

    mgr._execute_background_task = _exec_bg
    return mgr, scheduled


@pytest.mark.asyncio
async def test_worker_death_below_threshold_does_nothing():
    mgr, scheduled = _death_manager(
        state=MState.EXECUTING_QUEUE, environment_exists=True, limit=3, alive=False
    )
    # Two timeouts (< limit of 3): count increments, no recovery.
    await mgr._handle_possible_worker_death()
    await mgr._handle_possible_worker_death()
    assert mgr._worker_state_timeout_count == 2
    assert mgr._worker_death_handled is False
    assert scheduled["kills"] == 0


@pytest.mark.asyncio
async def test_worker_death_alive_worker_not_killed():
    """Reaching the timeout threshold but the watchdog says the process is alive
    (slow, not dead) must NOT trigger recovery."""
    mgr, scheduled = _death_manager(
        state=MState.EXECUTING_QUEUE, environment_exists=True, limit=1, alive=True
    )
    await mgr._handle_possible_worker_death()  # limit=1 -> checks watchdog immediately
    assert mgr._worker_death_handled is False
    assert scheduled["kills"] == 0


@pytest.mark.asyncio
async def test_worker_death_executing_schedules_kill():
    mgr, scheduled = _death_manager(
        state=MState.EXECUTING_QUEUE, environment_exists=True, limit=1, alive=False
    )
    await mgr._handle_possible_worker_death()
    # The kill/cleanup runs as a scheduled background task.
    for _ in range(5):
        if scheduled["kills"]:
            break
        await asyncio.sleep(0)
    assert scheduled["kills"] == 1
    assert mgr._worker_death_handled is True


@pytest.mark.asyncio
async def test_worker_death_handled_only_once():
    mgr, scheduled = _death_manager(
        state=MState.EXECUTING_QUEUE, environment_exists=True, limit=1, alive=False
    )
    await mgr._handle_possible_worker_death()
    await mgr._handle_possible_worker_death()  # already handled -> no-op
    for _ in range(5):
        await asyncio.sleep(0)
    assert scheduled["kills"] == 1


class _FakeWatchdogComm:
    def __init__(self, *, result=None, exc=None):
        self._result = result
        self._exc = exc

    async def send_msg(self, method):
        if self._exc is not None:
            raise self._exc
        return self._result


@pytest.mark.asyncio
async def test_watchdog_confirm_worker_dead_semantics():
    """Only a *positive* 'worker_alive: False' confirms death; a comm error /
    timeout or an unexpected shape means 'cannot confirm' -> not dead (keep
    waiting), never a false-positive kill."""
    mgr = RunEngineManager.__new__(RunEngineManager)

    mgr._comm_to_watchdog = _FakeWatchdogComm(result={"worker_alive": False})
    assert await mgr._watchdog_confirm_worker_dead() is True

    mgr._comm_to_watchdog = _FakeWatchdogComm(result={"worker_alive": True})
    assert await mgr._watchdog_confirm_worker_dead() is False

    # Watchdog comm timeout -> cannot confirm -> not dead.
    mgr._comm_to_watchdog = _FakeWatchdogComm(exc=CommTimeoutError("timeout"))
    assert await mgr._watchdog_confirm_worker_dead() is False

    # Unexpected response shape -> conservative 'alive' -> not dead.
    mgr._comm_to_watchdog = _FakeWatchdogComm(result={})
    assert await mgr._watchdog_confirm_worker_dead() is False


@pytest.mark.asyncio
async def test_worker_death_not_recovered_when_watchdog_unreachable():
    """If the watchdog can't be reached to confirm death, recovery must not run
    (uses the real _watchdog_confirm_worker_dead with a raising watchdog comm)."""
    mgr = RunEngineManager.__new__(RunEngineManager)
    mgr._environment_exists = True
    mgr._manager_state = MState.EXECUTING_QUEUE
    mgr._worker_state_timeout_count = 0
    mgr._worker_state_timeout_limit = 1
    mgr._worker_death_handled = False
    mgr._loop = asyncio.get_running_loop()
    mgr._comm_to_watchdog = _FakeWatchdogComm(exc=CommTimeoutError("timeout"))

    killed = {"n": 0}

    async def _kill():
        killed["n"] += 1

    mgr._kill_re_worker_task = _kill

    async def _exec_bg(coro):
        return await coro

    mgr._execute_background_task = _exec_bg

    await mgr._handle_possible_worker_death()
    for _ in range(5):
        await asyncio.sleep(0)
    assert killed["n"] == 0
    assert mgr._worker_death_handled is False


@pytest.mark.asyncio
async def test_worker_death_during_creating_resolves_future_false():
    """Worker death during env-open unblocks the open await with False so its
    existing failure path cleans up (rather than hanging forever)."""
    mgr, scheduled = _death_manager(
        state=MState.CREATING_ENVIRONMENT, environment_exists=False, limit=1, alive=False
    )
    mgr._fut_manager_task_completed = asyncio.get_running_loop().create_future()
    await mgr._handle_possible_worker_death()
    assert mgr._fut_manager_task_completed.result() is False
    assert scheduled["kills"] == 0  # env-open path, not the kill path
    assert mgr._worker_death_handled is True


@pytest.mark.asyncio
async def test_worker_death_during_closing_resolves_future_true():
    mgr, scheduled = _death_manager(
        state=MState.CLOSING_ENVIRONMENT, environment_exists=True, limit=1, alive=False
    )
    mgr._fut_manager_task_completed = asyncio.get_running_loop().create_future()
    await mgr._handle_possible_worker_death()
    assert mgr._fut_manager_task_completed.result() is True
    assert scheduled["kills"] == 0


@pytest.mark.asyncio
async def test_worker_death_ignored_when_no_environment():
    mgr, scheduled = _death_manager(
        state=MState.IDLE, environment_exists=False, limit=1, alive=False
    )
    await mgr._handle_possible_worker_death()
    assert mgr._worker_death_handled is False
    assert scheduled["kills"] == 0


@pytest.mark.asyncio
async def test_good_state_response_resets_timeout_counter():
    """A successful state poll resets the consecutive-timeout counter."""
    mgr = RunEngineManager.__new__(RunEngineManager)
    mgr._environment_exists = True
    mgr._manager_state = MState.IDLE
    mgr._worker_state_timeout_count = 4  # some prior timeouts
    mgr._worker_state_timeout_limit = 5
    mgr._worker_death_handled = False
    mgr._loop = asyncio.get_running_loop()
    mgr._re_pause_pending = True
    mgr._worker_state_info = None
    mgr._exec_loop_deactivated_event = asyncio.Event()
    mgr._exec_loop_deactivated_event.set()
    mgr._status_update = lambda: None

    ws = {
        "re_state": "idle",
        "plans_and_devices_list_updated": False,
        "completed_tasks_available": False,
        "unexpected_shutdown": False,
        "ip_kernel_captured": False,
        "environment_state": "idle",
        "re_report_available": False,
        "run_list_updated": False,
    }

    async def fake_state():
        return ws, ""

    mgr._worker_request_state = fake_state

    await mgr._periodic_worker_state_request_once()
    assert mgr._worker_state_timeout_count == 0


# ----------------------------------------------------------------------------
# Late pipe response must not raise on a done/cancelled future
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_response_received_ignores_late_response_on_cancelled_future():
    conn1, conn2 = multiprocessing.Pipe()
    try:
        comm = PipeJsonRpcSendAsync(conn=conn1, use_json=False, name="test")
        # Simulate a request that already timed out: expected id set, future
        # cancelled by wait_for.
        comm._expected_msg_id = "abc"
        fut = asyncio.get_running_loop().create_future()
        fut.cancel()
        comm._fut_recv = fut

        # A late response with the matching id must not raise InvalidStateError.
        await comm._response_received({"id": "abc", "result": 1})
        assert comm._expected_msg_id is None
    finally:
        conn1.close()
        conn2.close()


@pytest.mark.asyncio
async def test_response_received_ignores_duplicate_on_done_future():
    conn1, conn2 = multiprocessing.Pipe()
    try:
        comm = PipeJsonRpcSendAsync(conn=conn1, use_json=False, name="test")
        comm._expected_msg_id = "def"
        fut = asyncio.get_running_loop().create_future()
        fut.set_result({"id": "def", "result": 1})
        comm._fut_recv = fut

        # A duplicate response on an already-resolved future must not raise.
        await comm._response_received({"id": "def", "result": 2})
        assert comm._expected_msg_id is None
        assert fut.result() == {"id": "def", "result": 1}  # unchanged
    finally:
        conn1.close()
        conn2.close()


@pytest.mark.asyncio
async def test_background_task_exception_is_retrieved_by_done_callback():
    """A detached pipe callback task that raises must have its exception
    retrieved (and the task discarded) by the done-callback, so it doesn't
    surface as an unretrieved-task warning."""
    conn1, conn2 = multiprocessing.Pipe()
    try:
        comm = PipeJsonRpcSendAsync(conn=conn1, use_json=False, name="test")

        async def boom():
            raise RuntimeError("boom")

        task = asyncio.create_task(boom())
        comm._background_tasks.add(task)
        task.add_done_callback(comm._on_background_task_done)

        # Let the task run and the done-callback fire (it retrieves the exception).
        for _ in range(10):
            if task.done():
                break
            await asyncio.sleep(0.01)

        assert task.done()
        assert task not in comm._background_tasks
        # The exception was retrieved by the callback; re-reading is idempotent.
        assert isinstance(task.exception(), RuntimeError)
    finally:
        conn1.close()
        conn2.close()


@pytest.mark.asyncio
async def test_background_task_cancelled_is_handled_by_done_callback():
    conn1, conn2 = multiprocessing.Pipe()
    try:
        comm = PipeJsonRpcSendAsync(conn=conn1, use_json=False, name="test")

        async def sleeper():
            await asyncio.sleep(10)

        task = asyncio.create_task(sleeper())
        comm._background_tasks.add(task)
        task.add_done_callback(comm._on_background_task_done)
        await asyncio.sleep(0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        # Cancelled task is discarded without touching .exception() (which would raise).
        assert task not in comm._background_tasks
    finally:
        conn1.close()
        conn2.close()
