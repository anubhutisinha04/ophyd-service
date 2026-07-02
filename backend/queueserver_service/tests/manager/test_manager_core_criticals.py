"""Regression tests for the queueserver manager-core criticals fixed in the
2026-07 backend review (reports 00-summary / 07-queueserver-manager-core):

* C1 — the worker-state poll task must not die permanently on a double
  ``set_result`` (``_complete_manager_task`` guard) and the poll loop must
  survive an exception in its body.
* C2 — command-handler execution must be serialized by a single dispatch lock
  so concurrent 0MQ / HTTP-loopback / autostart requests can't interleave their
  check-then-act state transitions.
* H1 — the plan report must be fetched with the long pipe timeout, retried once
  on timeout, and retained by the worker across a read (so a lost pipe response
  can be re-fetched instead of marking a completed plan failed).
* H4 — the multiprocessing-pipe receive threads must exit (not busy-spin at
  100% CPU) when the peer closes the pipe.

The manager/worker are constructed via ``__new__`` (bypassing the
multiprocessing-heavy ``__init__``); only the attributes each method under test
touches are populated.
"""

from __future__ import annotations

import asyncio
import contextlib
import multiprocessing
import threading

import pytest

from queueserver_service.common.comms import (
    CommTimeoutError,
    PipeJsonRpcReceive,
    PipeJsonRpcSendAsync,
)
from queueserver_service.manager.manager import RunEngineManager
from queueserver_service.manager.manager import MState
from queueserver_service.manager.worker import PlanExecState, RunEngineWorker


# ----------------------------------------------------------------------------
# C1 — poll-task resilience
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_manager_task_guards_double_set_result():
    """A second resolution of the completion future (poll loop re-entering the
    CREATING/CLOSING_ENVIRONMENT branch after it was already resolved) must be a
    no-op, not an ``InvalidStateError`` that would kill the poll task."""
    mgr = RunEngineManager.__new__(RunEngineManager)
    mgr._fut_manager_task_completed = asyncio.get_running_loop().create_future()

    mgr._complete_manager_task(True)
    assert mgr._fut_manager_task_completed.result() is True

    # Would raise InvalidStateError without the guard.
    mgr._complete_manager_task(False)
    assert mgr._fut_manager_task_completed.result() is True


@pytest.mark.asyncio
async def test_complete_manager_task_handles_none_future():
    """No future in flight (e.g. kill requested before env open started) must
    not raise."""
    mgr = RunEngineManager.__new__(RunEngineManager)
    mgr._fut_manager_task_completed = None
    mgr._complete_manager_task(True)  # must not raise


@pytest.mark.asyncio
async def test_periodic_worker_state_loop_survives_exceptions(monkeypatch):
    """An exception raised by the loop body must be logged and swallowed so the
    poll task keeps running (previously it died permanently, silently bricking
    the manager while heartbeats kept the watchdog satisfied)."""
    mgr = RunEngineManager.__new__(RunEngineManager)

    calls = {"n": 0}
    done = asyncio.Event()

    async def fake_once():
        calls["n"] += 1
        if calls["n"] <= 3:
            raise RuntimeError("boom")
        done.set()

    mgr._periodic_worker_state_request_once = fake_once

    real_sleep = asyncio.sleep

    async def fast_sleep(_t):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    task = asyncio.ensure_future(mgr._periodic_worker_state_request())
    try:
        await asyncio.wait_for(done.wait(), timeout=5.0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert calls["n"] >= 4  # loop continued past the three exceptions


def _idle_ws():
    return {
        "re_state": "idle",
        "plans_and_devices_list_updated": False,
        "completed_tasks_available": False,
        "unexpected_shutdown": False,
        "ip_kernel_captured": False,
        "environment_state": "idle",
        "re_report_available": False,
        "run_list_updated": False,
    }


@pytest.mark.asyncio
async def test_poll_once_completes_env_open_and_survives_reentry():
    """Exercises the real poll-iteration body for the exact C1 scenario: env-open
    resolves the completion future while the manager is still in
    CREATING_ENVIRONMENT (env-open holds that state while it runs the
    plans/devices download + config-service sync), so the next poll iteration
    re-enters the same branch and must not raise ``InvalidStateError`` on the
    already-resolved future."""
    mgr = RunEngineManager.__new__(RunEngineManager)
    mgr._environment_exists = False
    mgr._manager_state = MState.CREATING_ENVIRONMENT
    mgr._use_ipython_kernel = False
    mgr._worker_state_info = None
    mgr._exec_loop_deactivated_event = asyncio.Event()
    mgr._exec_loop_deactivated_event.set()  # skip the IPython-kernel branch
    mgr._fut_manager_task_completed = asyncio.get_running_loop().create_future()
    mgr._status_update = lambda: None

    ws = _idle_ws()

    async def fake_state():
        return ws, ""

    mgr._worker_request_state = fake_state

    # First poll resolves the env-open future (worker reports 'idle').
    await mgr._periodic_worker_state_request_once()
    assert mgr._fut_manager_task_completed.result() is True

    # Second poll re-enters the CREATING_ENVIRONMENT branch — must be a no-op,
    # not an InvalidStateError.
    await mgr._periodic_worker_state_request_once()
    assert mgr._fut_manager_task_completed.result() is True


# ----------------------------------------------------------------------------
# C2 — dispatch serialization
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_command_serializes_handlers():
    """Two concurrent ``_dispatch_command`` calls must run one after another,
    not interleave — this is the serialization the state machine assumes and
    that unified HTTP mode otherwise breaks."""
    mgr = RunEngineManager.__new__(RunEngineManager)
    mgr._command_dispatch_lock = None  # lazily created by _get_dispatch_lock

    events = []

    async def handler(manager, params):
        events.append(("enter", params["id"]))
        await asyncio.sleep(0.05)
        events.append(("exit", params["id"]))
        return {"success": True}

    mgr._command_handlers = {"h": handler}

    await asyncio.gather(
        mgr._dispatch_command("h", {"id": 1}),
        mgr._dispatch_command("h", {"id": 2}),
    )

    # Serialized => each enter is immediately followed by its own exit.
    assert events in (
        [("enter", 1), ("exit", 1), ("enter", 2), ("exit", 2)],
        [("enter", 2), ("exit", 2), ("enter", 1), ("exit", 1)],
    )


@pytest.mark.asyncio
async def test_dispatch_command_unknown_method_needs_no_lock():
    """The unknown-method path returns before acquiring the lock (and before
    _get_dispatch_lock is ever needed)."""
    mgr = RunEngineManager.__new__(RunEngineManager)
    mgr._command_dispatch_lock = None
    mgr._command_handlers = {}

    resp = await mgr._dispatch_command("does_not_exist", {})
    assert resp["success"] is False
    assert "Unknown method" in resp["msg"]


@pytest.mark.asyncio
async def test_dispatch_command_surfaces_handler_exception_as_failure():
    mgr = RunEngineManager.__new__(RunEngineManager)
    mgr._command_dispatch_lock = None

    async def boom(manager, params):
        raise RuntimeError("kaboom")

    mgr._command_handlers = {"boom": boom}

    resp = await mgr._dispatch_command("boom", {})
    assert resp["success"] is False
    assert resp["msg"] == "kaboom"


# ----------------------------------------------------------------------------
# H1 — plan-report loss (manager side: long timeout + one retry)
# ----------------------------------------------------------------------------


class _FakeComm:
    def __init__(self, responses):
        # responses: list of either an exception instance or a return value
        self._responses = list(responses)
        self.calls = []

    async def send_msg(self, method, timeout=None):
        self.calls.append((method, timeout))
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.mark.asyncio
async def test_plan_report_uses_long_timeout_and_retries_once():
    mgr = RunEngineManager.__new__(RunEngineManager)
    mgr._comm_to_worker_timeout_long = 10
    report = {"plan_state": "completed", "success": True}
    mgr._comm_to_worker = _FakeComm([CommTimeoutError("timeout"), report])

    result, err = await mgr._worker_request_plan_report()

    assert result == report
    assert err == ""
    # Both attempts use the long timeout.
    assert mgr._comm_to_worker.calls == [
        ("request_plan_report", 10),
        ("request_plan_report", 10),
    ]


@pytest.mark.asyncio
async def test_plan_report_gives_up_after_one_retry():
    mgr = RunEngineManager.__new__(RunEngineManager)
    mgr._comm_to_worker_timeout_long = 10
    mgr._comm_to_worker = _FakeComm([CommTimeoutError("t1"), CommTimeoutError("t2")])

    result, err = await mgr._worker_request_plan_report()

    assert result is None
    assert "Timeout" in err
    assert len(mgr._comm_to_worker.calls) == 2  # exactly one retry


@pytest.mark.asyncio
async def test_plan_report_not_available_is_not_retried():
    """When the worker explicitly returns ``None`` (no report), that is not a
    timeout and must not trigger a retry."""
    mgr = RunEngineManager.__new__(RunEngineManager)
    mgr._comm_to_worker_timeout_long = 10
    mgr._comm_to_worker = _FakeComm([None])

    result, err = await mgr._worker_request_plan_report()

    assert result is None
    assert "not available" in err
    assert len(mgr._comm_to_worker.calls) == 1


# ----------------------------------------------------------------------------
# H1 — plan-report loss (worker side: report retained across a read)
# ----------------------------------------------------------------------------


def _bare_worker():
    w = RunEngineWorker.__new__(RunEngineWorker)
    w._re_report_lock = threading.Lock()
    return w


def test_worker_plan_report_retained_and_marked_delivered_on_read():
    w = _bare_worker()
    report = {"plan_state": "completed", "success": True}
    w._re_report = report
    w._re_report_delivered = False

    # Not-yet-delivered report is advertised as available.
    assert (w._re_report is not None and not w._re_report_delivered) is True

    assert w._request_plan_report_handler() == report
    assert w._re_report_delivered is True
    assert w._re_report is report  # retained, NOT cleared on read

    # A retry (e.g. after a lost pipe response) still returns the report.
    assert w._request_plan_report_handler() == report

    # A delivered report is no longer advertised as available (no re-processing).
    assert (w._re_report is not None and not w._re_report_delivered) is False


def test_worker_reset_clears_report_and_delivered_flag():
    w = _bare_worker()
    w._re_report = {"plan_state": "completed"}
    w._re_report_delivered = True
    w._running_plan_info = {"item_uid": "x"}
    w._running_plan_exec_state = None
    # '_RE' is unset on a bare worker, so 're_state' returns None -> reset proceeds.

    result = w._command_reset_worker_handler()

    assert result["status"] == "accepted"
    assert w._re_report is None
    assert w._re_report_delivered is False
    assert w._running_plan_exec_state == PlanExecState.RESET


# ----------------------------------------------------------------------------
# H4 — pipe-EOF busy-spin
# ----------------------------------------------------------------------------


def test_pipe_receive_thread_exits_when_peer_closes():
    """PipeJsonRpcReceive's receive thread must exit promptly when the peer end
    of the pipe is closed, instead of busy-spinning on a ready poll() forever."""
    conn1, conn2 = multiprocessing.Pipe()
    comm = PipeJsonRpcReceive(conn=conn1, use_json=False, name="test-recv")
    comm.start()
    recv_thread = comm._thread_conn
    assert recv_thread.is_alive()

    conn2.close()  # peer close -> conn1.recv() raises EOFError

    recv_thread.join(timeout=5.0)
    assert not recv_thread.is_alive()
    assert comm._thread_running is False
    comm.stop()


@pytest.mark.asyncio
async def test_pipe_send_async_receive_thread_exits_when_peer_closes():
    """Same guarantee for PipeJsonRpcSendAsync's receive thread."""
    conn1, conn2 = multiprocessing.Pipe()
    comm = PipeJsonRpcSendAsync(conn=conn1, use_json=False, name="test-send")
    comm.start()
    recv_thread = comm._pipe_recv_thread
    assert recv_thread.is_alive()

    conn2.close()

    for _ in range(50):
        if not recv_thread.is_alive():
            break
        await asyncio.sleep(0.1)

    assert not recv_thread.is_alive()
    assert comm._thread_running is False
    comm.stop()
