"""Tests for acknowledged download of completed-task results.

The worker keeps completed-task results until the manager acknowledges them
(by sending back the ``task_uid``s it received on the previous download). This
makes the transfer resilient to a lost pipe response: the results stay available
for re-fetching instead of being cleared on the first read.

The manager/worker are built via ``__new__`` (bypassing the multiprocessing-heavy
``__init__``); only the attributes each method under test touches are populated.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from queueserver_service.common.comms import CommTimeoutError
from queueserver_service.manager.manager import MState, RunEngineManager
from queueserver_service.manager.worker import RunEngineWorker


# ----------------------------------------------------------------------------
# Worker side: retain until acknowledged
# ----------------------------------------------------------------------------


def _worker():
    w = RunEngineWorker.__new__(RunEngineWorker)
    w._completed_tasks_lock = threading.Lock()
    w._completed_tasks = []
    return w


def test_worker_retains_results_until_acked():
    w = _worker()
    w._completed_tasks = [{"task_uid": "A"}, {"task_uid": "B"}]

    # First read (no ack) returns all and retains all.
    out = w._request_task_results_handler()
    assert [t["task_uid"] for t in out["task_results"]] == ["A", "B"]
    assert [t["task_uid"] for t in w._completed_tasks] == ["A", "B"]

    # A re-read (e.g. after a lost response) still returns the same results.
    out2 = w._request_task_results_handler()
    assert [t["task_uid"] for t in out2["task_results"]] == ["A", "B"]

    # Acknowledging A drops it and returns the remainder.
    out3 = w._request_task_results_handler(ack_uids=["A"])
    assert [t["task_uid"] for t in out3["task_results"]] == ["B"]
    assert [t["task_uid"] for t in w._completed_tasks] == ["B"]

    # Acknowledging B empties the list.
    out4 = w._request_task_results_handler(ack_uids=["B"])
    assert out4["task_results"] == []
    assert w._completed_tasks == []


def test_worker_ack_of_unknown_uid_is_noop():
    w = _worker()
    w._completed_tasks = [{"task_uid": "A"}]
    out = w._request_task_results_handler(ack_uids=["does-not-exist"])
    assert [t["task_uid"] for t in out["task_results"]] == ["A"]
    assert [t["task_uid"] for t in w._completed_tasks] == ["A"]


# ----------------------------------------------------------------------------
# Manager side: acknowledgement tracking + lost-response resilience
# ----------------------------------------------------------------------------


class _FakeWorkerComm:
    """Simulates the worker's ack-based task-results handler over the pipe."""

    def __init__(self):
        self._completed = []  # list of {"task_uid": ...}
        self.fail_next = False  # simulate a dropped response
        self.acks_seen = []

    def add(self, *uids):
        for uid in uids:
            self._completed.append({"task_uid": uid, "payload": uid})

    @property
    def held_uids(self):
        return [t["task_uid"] for t in self._completed]

    async def send_msg(self, method, params=None, *, timeout=None):
        assert method == "request_task_results"
        ack = set((params or {}).get("ack_uids", []))
        self.acks_seen.append(sorted(ack))
        # Worker drops acknowledged results (inbound processed) ...
        self._completed = [t for t in self._completed if t["task_uid"] not in ack]
        if self.fail_next:
            # ... but the response is lost before the manager receives it.
            self.fail_next = False
            raise CommTimeoutError("timeout")
        return {"task_results": [dict(t) for t in self._completed]}


def _results_manager(comm):
    mgr = RunEngineManager.__new__(RunEngineManager)
    mgr._task_results_received_uids = []
    mgr._comm_to_worker_timeout_long = 10
    mgr._manager_state = MState.IDLE
    mgr._running_task_uid = None
    mgr._loop = asyncio.get_running_loop()
    mgr._status_update = lambda: None
    mgr._comm_to_worker = comm

    stored = []

    class _FakeTaskResults:
        async def add_completed_task(self, *, task_uid, payload=None):
            stored.append(task_uid)

    mgr._task_results = _FakeTaskResults()
    mgr._loading_task_results = False
    return mgr, stored


@pytest.mark.asyncio
async def test_manager_acknowledges_received_results_on_next_download():
    comm = _FakeWorkerComm()
    comm.add("A")
    mgr, stored = _results_manager(comm)

    # First download: empty ack, receives + stores A, remembers it to ack next.
    await mgr._load_task_results_from_worker()
    assert stored == ["A"]
    assert mgr._task_results_received_uids == ["A"]
    assert comm.acks_seen[-1] == []
    assert comm.held_uids == ["A"]  # retained until acknowledged

    # Second download: acknowledges A, worker drops it, nothing new.
    await mgr._load_task_results_from_worker()
    assert comm.acks_seen[-1] == ["A"]
    assert comm.held_uids == []
    assert stored == ["A"]  # not re-stored
    assert mgr._task_results_received_uids == []


@pytest.mark.asyncio
async def test_manager_does_not_lose_results_on_dropped_response():
    comm = _FakeWorkerComm()
    comm.add("B")
    mgr, stored = _results_manager(comm)

    # Download whose response is dropped: B is not stored, stays available.
    comm.fail_next = True
    await mgr._load_task_results_from_worker()
    assert "B" not in stored
    assert comm.held_uids == ["B"]
    assert mgr._task_results_received_uids == []  # nothing acknowledged

    # Next download re-fetches and stores B.
    await mgr._load_task_results_from_worker()
    assert stored == ["B"]
    assert mgr._task_results_received_uids == ["B"]


@pytest.mark.asyncio
async def test_manager_skips_reprocessing_already_received_results():
    """If the worker re-delivers a result the manager already received (e.g. a
    previous acknowledgement was lost), it is acknowledged again but not stored
    twice."""
    comm = _FakeWorkerComm()
    comm.add("C")
    mgr, stored = _results_manager(comm)

    # Pretend C was already received but not yet acknowledged, and the worker
    # still holds it (the acknowledgement round was lost).
    mgr._task_results_received_uids = ["C"]

    # The worker would drop C on ack; force a re-delivery to exercise the guard.
    async def send_msg(method, params=None, *, timeout=None):
        return {"task_results": [{"task_uid": "C", "payload": "C"}]}

    comm.send_msg = send_msg

    await mgr._load_task_results_from_worker()
    assert stored == []  # C not re-stored
    assert mgr._task_results_received_uids == ["C"]


@pytest.mark.asyncio
async def test_load_task_results_is_single_flight():
    """Only one download runs at a time; a concurrent call is a no-op so the
    poll loop can't flood the worker pipe while results stay available."""
    comm = _FakeWorkerComm()
    comm.add("A")
    mgr, stored = _results_manager(comm)

    started = asyncio.Event()
    release = asyncio.Event()
    original_impl = mgr._load_task_results_from_worker_impl

    async def blocking_impl():
        started.set()
        await release.wait()
        await original_impl()

    mgr._load_task_results_from_worker_impl = blocking_impl

    first = asyncio.create_task(mgr._load_task_results_from_worker())
    await started.wait()

    # A second concurrent call returns immediately without starting a download.
    await mgr._load_task_results_from_worker()
    assert stored == []

    release.set()
    await first
    assert stored == ["A"]
