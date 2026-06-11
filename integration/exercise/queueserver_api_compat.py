#!/usr/bin/env python3
"""Compatibility exerciser: the PyPI ``bluesky-queueserver-api`` client library
against the in-tree queueserver_service.

queueserver_service is maintained independently of upstream bluesky-queueserver,
but the wire surfaces consumed by bluesky-queueserver-api are a frozen public
contract (see backend/queueserver_service/README.md). This script drives the
running with-queueserver pod through the REAL client package over BOTH
transports — 0MQ (CONTROL + INFO/PUB) and HTTP — so contract drift fails CI.

Run against integration/pods/with-queueserver (ports 60610/60615/60625 on
localhost):

    pip install bluesky-queueserver-api
    python integration/exercise/queueserver_api_compat.py
"""

import os
import sys

API_KEY = os.environ.get("QSERVER_API_KEY", "mad")
HTTP_URI = os.environ.get("QSERVER_HTTP_URI", "http://localhost:60610")
ZMQ_CONTROL = os.environ.get("QSERVER_ZMQ_CONTROL", "tcp://localhost:60615")
ZMQ_INFO = os.environ.get("QSERVER_ZMQ_INFO", "tcp://localhost:60625")

_failures = []


def check(label, fn):
    try:
        fn()
        print(f"  ok  {label}")
    except Exception as exc:
        print(f"FAIL  {label}: {type(exc).__name__}: {exc}", file=sys.stderr)
        _failures.append(label)


def expect(cond, msg):
    if not cond:
        raise AssertionError(msg)


def monitor_sees_message(monitor, timeout_total=30, stimulus=None):
    """Poll a console/system-info monitor until any message arrives.

    ``stimulus`` is invoked each cycle to provoke output (the console topic is
    silent unless the manager actually logs something).
    """
    import time

    from bluesky_queueserver_api.comm_base import RequestTimeoutError

    monitor.enable()
    deadline = time.monotonic() + timeout_total
    while time.monotonic() < deadline:
        if stimulus is not None:
            stimulus()
        try:
            msg = monitor.next_msg(timeout=2)
            expect(msg, "monitor returned an empty message")
            return
        except RequestTimeoutError:
            continue
    raise AssertionError(f"no message within {timeout_total}s")


def poke_http_status():
    """Hit /api/status over HTTP; the uvicorn access-log line lands on the
    manager's console output stream."""
    import urllib.request

    req = urllib.request.Request(
        f"{HTTP_URI}/api/status", headers={"Authorization": f"ApiKey {API_KEY}"})
    urllib.request.urlopen(req, timeout=5).read()


def main():
    from bluesky_queueserver_api import BPlan
    from bluesky_queueserver_api.http import REManagerAPI as HttpRM
    from bluesky_queueserver_api.zmq import REManagerAPI as ZmqRM

    print("== 0MQ transport (CONTROL request/response) ==")
    zmq_rm = ZmqRM(zmq_control_addr=ZMQ_CONTROL, zmq_info_addr=ZMQ_INFO)

    check("status() over 0MQ", lambda: expect(
        "manager_state" in zmq_rm.status(), "status missing manager_state"))
    check("queue_clear() over 0MQ", zmq_rm.queue_clear)
    check("item_add(BPlan) over 0MQ", lambda: expect(
        zmq_rm.item_add(BPlan("count", ["det1"], num=1))["success"],
        "item_add rejected"))
    check("queue_get() over 0MQ sees the item", lambda: expect(
        len(zmq_rm.queue_get()["items"]) == 1, "queue does not show 1 item"))
    check("plans_allowed() over 0MQ", lambda: expect(
        "count" in zmq_rm.plans_allowed()["plans_allowed"],
        "'count' not in plans_allowed"))

    print("== 0MQ INFO (PUB) channel: console + system-info monitors ==")
    check("console_monitor receives a message",
          lambda: monitor_sees_message(zmq_rm.console_monitor,
                                       stimulus=poke_http_status))
    if hasattr(zmq_rm, "system_info_monitor"):
        check("system_info_monitor receives a message",
              lambda: monitor_sees_message(zmq_rm.system_info_monitor))
    else:
        print("  --  system_info_monitor not in this client release; skipped")

    print("== HTTP transport ==")
    http_rm = HttpRM(http_server_uri=HTTP_URI)
    http_rm.set_authorization_key(api_key=API_KEY)

    check("status() over HTTP", lambda: expect(
        "manager_state" in http_rm.status(), "status missing manager_state"))
    check("queue_get() over HTTP sees the 0MQ-added item", lambda: expect(
        len(http_rm.queue_get()["items"]) == 1,
        "item added over 0MQ not visible over HTTP"))
    check("devices_allowed() over HTTP", lambda: expect(
        http_rm.devices_allowed()["devices_allowed"], "devices_allowed empty"))
    check("queue_clear() over HTTP", http_rm.queue_clear)

    def zmq_sees_clear():
        # The client caches queue_get on the status plan_queue_uid, and status
        # itself is cached briefly — poll instead of asserting instantly.
        import time

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if len(zmq_rm.queue_get()["items"]) == 0:
                return
            time.sleep(0.5)
        raise AssertionError("HTTP clear not visible over 0MQ within 10s")

    check("queue_get() over 0MQ sees the HTTP clear", zmq_sees_clear)

    zmq_rm.close()
    http_rm.close()

    if _failures:
        print(f"\n{len(_failures)} bluesky-queueserver-api compatibility "
              f"check(s) FAILED: {_failures}", file=sys.stderr)
        sys.exit(1)
    print("\nAll bluesky-queueserver-api compatibility checks passed.")


if __name__ == "__main__":
    main()
