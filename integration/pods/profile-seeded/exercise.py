#!/usr/bin/env python3
"""End-to-end exerciser for the profile-seeded pod (run after
`docker compose up --build -d`; needs `pip install httpx`).

Covers the three Side-B integration stories:

1. Profile-collection bootstrap — the queueserver reads the mounted test
   profile collection on env-open and seeds the EMPTY config-service
   registry with the profile's devices.
2. Registry as source of truth — a device added to config-service via CRUD
   (never present in the profile) becomes usable in plans through the
   pre-plan staleness overlay, and survives an env close/reopen
   (consume-mode cold start against a populated registry).
3. Per-plan device locking — while a plan executes, exactly the devices it
   references are locked (lock_scope: plan); direct-control refuses ophyd
   verbs / PV writes on them with HTTP 423 and allows unrelated devices;
   with the lock_all policy enabled, unrelated devices are refused too.
   Locks are released when the plan finishes.

Exits non-zero on the first failed check.
"""

import sys
import time

import httpx

QS = "http://localhost:60610"
CONFIG = "http://localhost:8004"
DC = "http://localhost:8003"
AUTH = {"Authorization": "ApiKey mad"}

# Devices defined in integration/profile_collections/test_collection/10-devices.py
PROFILE_DEVICES = {
    "det_spot", "det_pinhole", "det_edge",
    "sample_x", "sample_y", "ph_motor", "edge_motor", "ring_current",
}

# Device registered via CRUD in part 2 — intentionally absent from the profile.
EXTRA_DEVICE = {
    "metadata": {
        "name": "extra_signal",
        "device_label": "signal",
        "ophyd_class": "EpicsSignal",
        "module": "ophyd.signal",
        "is_readable": True,
        "is_subscribable": True,
        "pvs": {"extra_signal": "mini:slit:det"},
        "labels": ["runtime-registered"],
        "documentation": "Registered via CRUD after bootstrap; proves the registry drives the worker.",
    },
    "instantiation_spec": {
        "name": "extra_signal",
        "device_class": "ophyd.signal.EpicsSignal",
        "args": ["mini:slit:det"],
        "kwargs": {"name": "extra_signal"},
        "active": True,
    },
}

http = httpx.Client(timeout=30.0)
_checks = 0


def ok(msg: str) -> None:
    global _checks
    _checks += 1
    print(f"  ok  {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"FAIL  {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def check(cond: bool, msg: str) -> None:
    ok(msg) if cond else fail(msg)


def wait_until(fn, msg: str, timeout: float = 60.0, interval: float = 0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = fn()
        if result:
            ok(msg)
            return result
        time.sleep(interval)
    fail(f"timed out after {timeout:.0f}s: {msg}")


def qs_status() -> dict:
    return http.get(f"{QS}/api/status", headers=AUTH).json()


def device_status(name: str) -> dict:
    r = http.get(f"{CONFIG}/api/v1/devices/{name}/status")
    if r.status_code != 200:
        fail(f"GET /devices/{name}/status -> {r.status_code}: {r.text}")
    return r.json()


def add_plan(name: str, args: list, kwargs: dict) -> None:
    r = http.post(
        f"{QS}/api/queue/item/add",
        headers=AUTH,
        json={"item": {"item_type": "plan", "name": name, "args": args, "kwargs": kwargs}},
    )
    if r.status_code != 200 or not r.json().get("success"):
        fail(f"queue/item/add {name} -> {r.status_code}: {r.text}")


def start_queue() -> None:
    r = http.post(f"{QS}/api/queue/start", headers=AUTH)
    if not r.json().get("success"):
        fail(f"queue/start -> {r.text}")


def wait_queue_done(msg: str, timeout: float = 120.0) -> None:
    wait_until(
        lambda: qs_status()["manager_state"] == "idle" and qs_status()["items_in_queue"] == 0,
        msg,
        timeout=timeout,
    )


def last_history_status() -> str:
    items = http.get(f"{QS}/api/history/get", headers=AUTH).json()["items"]
    if not items:
        fail("plan history is empty")
    return items[-1]["result"]["exit_status"]


def set_lock_all(value: bool) -> None:
    r = http.put(f"{CONFIG}/api/v1/devices/lock/policy", json={"lock_all": value})
    check(r.status_code == 200 and r.json()["lock_all"] is value, f"lock policy lock_all={value}")


def dc_execute(device: str, method: str, args: list, **extra) -> httpx.Response:
    return http.post(
        f"{DC}/api/v1/device/execute",
        json={"device_name": device, "method": method, "args": args, **extra},
        timeout=60.0,
    )


print("== health ==")
check(http.get(f"{CONFIG}/health").status_code == 200, "config_service /health")
check(http.get(f"{DC}/health").status_code == 200, "direct_control /health")
check(qs_status()["manager_state"] == "idle", "queueserver idle (unified mode)")

print("== 1. profile-collection bootstrap of an EMPTY registry ==")
check(http.get(f"{CONFIG}/api/v1/devices-info").json() == {}, "registry starts empty")

r = http.post(f"{QS}/api/environment/open", headers=AUTH)
check(r.json().get("success") is True, "environment/open accepted")
wait_until(lambda: qs_status()["worker_environment_exists"], "worker environment open", timeout=90)

# The env-open path awaits the config-service sync, so by the time the
# worker environment exists the bootstrap has either completed or failed.
wait_until(
    lambda: set(http.get(f"{CONFIG}/api/v1/devices-info").json()) == PROFILE_DEVICES or None,
    f"registry seeded with exactly the {len(PROFILE_DEVICES)} profile devices",
    timeout=30,
)

specs = http.get(f"{CONFIG}/api/v1/devices/instantiation").json()
check(specs["det_spot"]["device_class"] == "localdevs.Spot", "compound spec importable (localdevs.Spot)")
check(specs["sample_x"]["device_class"].endswith("EpicsMotor"), "motor spec importable (EpicsMotor)")

# lock_scope: plan — there must be NO environment-wide lock after env-open.
check(device_status("ph_motor")["available"] is True, "no env lock in plan scope (ph_motor available)")

print("== 2. registry is the device source from now on ==")
r = http.post(f"{CONFIG}/api/v1/devices", json=EXTRA_DEVICE)
check(r.status_code in (200, 201), "extra_signal registered via CRUD (never in the profile)")

# The pre-plan staleness check must pull extra_signal into the worker overlay,
# and the worker must instantiate it from the registry spec alone.
add_plan("count", [["extra_signal"]], {"num": 1})
start_queue()
wait_queue_done("count([extra_signal]) finished")
check(last_history_status() == "completed", "plan using the CRUD-registered device completed")

# Cold start against a populated registry: bootstrap must be skipped and the
# registry devices (incl. extra_signal) injected via consume mode.
check(http.post(f"{QS}/api/environment/close", headers=AUTH).json()["success"], "environment/close accepted")
wait_until(lambda: not qs_status()["worker_environment_exists"], "worker environment closed")
check(http.post(f"{QS}/api/environment/open", headers=AUTH).json()["success"], "environment/open #2 accepted")
wait_until(lambda: qs_status()["worker_environment_exists"], "worker environment reopened", timeout=90)

info = http.get(f"{CONFIG}/api/v1/devices-info").json()
check(set(info) == PROFILE_DEVICES | {"extra_signal"}, "reopen did not re-bootstrap (registry unchanged)")
allowed = http.get(f"{QS}/api/devices/allowed", headers=AUTH).json()["devices_allowed"]
check("extra_signal" in allowed, "extra_signal injected into reopened worker (consume mode)")

print("== 3a. per-plan locking enforced through direct-control ==")
set_lock_all(False)

# dwell_scan: 12 points x 0.5 s dwell -> ≥6 s window for mid-plan probes.
add_plan("dwell_scan", [["det_pinhole"], "ph_motor", -1, 1, 12], {"dwell": 0.5})
start_queue()

wait_until(
    lambda: device_status("ph_motor")["lock_status"] == "locked",
    "ph_motor locked for the plan",
    timeout=30,
    interval=0.2,
)
ph = device_status("ph_motor")
check(ph["locked_by_plan"] == "dwell_scan", "lock carries the plan name")
check(device_status("det_pinhole")["lock_status"] == "locked", "det_pinhole (also referenced) locked")
check(device_status("sample_x")["available"] is True, "sample_x (not in plan) stays available")

r = dc_execute("ph_motor", "set", [0.0], use_put=True)
check(r.status_code == 423, f"direct-control refuses ophyd verb on locked device (got {r.status_code})")
r = http.post(f"{DC}/api/v1/pv/set", json={"pv_name": "mini:ph:mtr", "value": 0.0})
check(r.status_code == 423, f"direct-control refuses PV write under locked device (got {r.status_code})")
r = dc_execute("sample_x", "set", [0.2])
check(r.status_code == 200 and r.json()["success"], "unrelated device still commandable mid-plan")

wait_queue_done("dwell_scan finished")
check(last_history_status() == "completed", "dwell_scan completed")
check(device_status("ph_motor")["available"] is True, "ph_motor released after the plan")
check(device_status("det_pinhole")["available"] is True, "det_pinhole released after the plan")

print("== 3b. lock_all policy: any plan locks every device ==")
set_lock_all(True)

add_plan("dwell_scan", [["det_pinhole"], "ph_motor", -1, 1, 12], {"dwell": 0.5})
start_queue()
wait_until(
    lambda: device_status("ph_motor")["lock_status"] == "locked",
    "ph_motor locked for the plan",
    timeout=30,
    interval=0.2,
)
check(device_status("sample_x")["available"] is False, "lock_all: sample_x reports unavailable too")
r = dc_execute("sample_x", "set", [0.4])
check(r.status_code == 423, f"lock_all: direct-control refuses unrelated device (got {r.status_code})")

wait_queue_done("dwell_scan #2 finished")
check(last_history_status() == "completed", "dwell_scan #2 completed")
check(device_status("sample_x")["available"] is True, "lock_all: everything available once locks drop")
r = dc_execute("sample_x", "set", [0.0])
check(r.status_code == 200 and r.json()["success"], "unrelated device commandable again after the plan")

set_lock_all(False)
check(http.post(f"{QS}/api/environment/close", headers=AUTH).json()["success"], "environment closed")

print(f"\nAll {_checks} profile-seeded exerciser checks passed.")
