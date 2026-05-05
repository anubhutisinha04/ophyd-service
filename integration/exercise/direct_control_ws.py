#!/usr/bin/env python3
"""
Exercise the direct_control_service WebSocket surface — both routes.

Companion to direct_control.sh, which deliberately skipped WebSockets
(bash + curl can't speak WS cleanly). This script covers the full WS
surface direct_control exposes:

  /api/v1/pv-socket      — PV-level subscriptions (finch's ophydSocketPVPath)
  /api/v1/device-socket  — device-level subscriptions (finch's ophydSocketDevicePath)

Both flows: connect → ping/pong → subscribe → first update → unsubscribe
→ close. If either fails, the script exits non-zero.

Endpoint protocols (from
backend/direct_control_service/src/direct_control/monitoring/):

  pv-socket
    Client → Server:  {"action": "subscribe",   "pv_names": ["..."]}
                      {"action": "unsubscribe", "pv_names": ["..."]}
                      {"action": "ping"}
    Server → Client:  {"type": "subscribed",   "pv_names": [...], ...}
                      {"type": "unsubscribed", "pv_names": [...], ...}
                      {"type": "pong" | "heartbeat" | "error", ...}
                      {"event_type": "pv_update", "pv": "...", ...}

  device-socket
    Client → Server:  {"action": "subscribe",   "device": "<name>"}
                      {"action": "unsubscribe", "device": "<name>"}
                      {"action": "ping"}
    Server → Client:  {"type": "subscribed",   "device": "<name>", ...}
                      {"type": "unsubscribed", "device": "<name>", ...}
                      {"type": "pong" | "error", ...}
                      {"event_type": "device_update", "device", "signal",
                       "value", ...}     (one frame per sub-PV change)

Dependency:
    pip install websockets   (>=12)
or  uv run --with websockets integration/exercise/direct_control_ws.py

Usage:
    ./direct_control_ws.py
    DIRECT_HOST=remote:8003 ./direct_control_ws.py
    PV_NAME=random_walk:x DEVICE_NAME=spot ./direct_control_ws.py

Exit 0 on full pass; non-zero on any failure.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, NoReturn

try:
    import websockets
except ImportError:
    sys.stderr.write(
        "error: `websockets` not installed.\n"
        "       pip install websockets   (or)   uv run --with websockets ...\n"
    )
    sys.exit(2)


DIRECT_HOST = os.environ.get("DIRECT_HOST", "localhost:8003")
PV_WS_URL = os.environ.get("DIRECT_WS_URL", f"ws://{DIRECT_HOST}/api/v1/pv-socket")
DEVICE_WS_URL = os.environ.get("DEVICE_WS_URL", f"ws://{DIRECT_HOST}/api/v1/device-socket")
PV_NAME = os.environ.get("PV_NAME", "mini:current")        # ticks frequently, in every pod
DEVICE_NAME = os.environ.get("DEVICE_NAME", "beam_current")  # single-PV device backed by mini:current
SUBSCRIBE_TIMEOUT = float(os.environ.get("SUBSCRIBE_TIMEOUT", "5"))
UPDATE_TIMEOUT = float(os.environ.get("UPDATE_TIMEOUT", "8"))


if sys.stdout.isatty():
    G, R, Y, B, X = "\033[32m", "\033[31m", "\033[33m", "\033[1m", "\033[0m"
else:
    G = R = Y = B = X = ""


def step(msg: str) -> None:
    print(f"\n{B}== {msg} =={X}")


def ok(msg: str) -> None:
    print(f"  {G}PASS{X}  {msg}")


def fail(msg: str) -> NoReturn:
    print(f"  {R}FAIL{X}  {msg}", file=sys.stderr)
    sys.exit(1)


async def recv_until(ws: Any, predicate, timeout: float, description: str) -> dict:
    """Receive frames until `predicate(msg)` returns truthy, ignoring others."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            fail(f"timeout ({timeout}s) waiting for {description}")
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except asyncio.TimeoutError:
            fail(f"timeout ({timeout}s) waiting for {description}")
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as e:
            fail(f"non-JSON frame received: {e}: {raw!r}")
        if predicate(msg):
            return msg
        # otherwise drop (typically heartbeat / unrelated update) and keep waiting


def is_event(msg: dict, type_: str) -> bool:
    return msg.get("type") == type_


def is_pv_update(msg: dict, pv_name: str | None = None) -> bool:
    if msg.get("event_type") != "pv_update":
        return False
    return pv_name is None or msg.get("pv") == pv_name


def is_device_update(msg: dict, device: str | None = None) -> bool:
    if msg.get("event_type") != "device_update":
        return False
    return device is None or msg.get("device") == device


async def exercise_pv_socket() -> None:
    print(f"\n{B}─── pv-socket ───{X}  url={PV_WS_URL}  pv={PV_NAME}")

    step("Connect")
    async with websockets.connect(PV_WS_URL) as ws:
        ok(f"connected to {PV_WS_URL}")

        step("Ping → pong")
        await ws.send(json.dumps({"action": "ping"}))
        await recv_until(ws, lambda m: is_event(m, "pong"),
                         SUBSCRIBE_TIMEOUT, "pong")
        ok("pong received")

        step(f"Subscribe to {PV_NAME}")
        await ws.send(json.dumps({"action": "subscribe", "pv_names": [PV_NAME]}))
        ack = await recv_until(ws, lambda m: is_event(m, "subscribed"),
                               SUBSCRIBE_TIMEOUT, "subscribed ack")
        if PV_NAME not in ack.get("pv_names", []):
            fail(f"subscribed ack missing {PV_NAME!r}: {ack}")
        ok(f"subscribed ack pv_names={ack.get('pv_names')}")

        step(f"Receive at least one pv_update for {PV_NAME}")
        update = await recv_until(ws, lambda m: is_pv_update(m, PV_NAME),
                                  UPDATE_TIMEOUT, f"pv_update for {PV_NAME}")
        for required in ("value", "timestamp", "connected"):
            if required not in update:
                fail(f"pv_update missing field {required!r}: {update}")
        ok(f"update received  value={update['value']!r}  connected={update['connected']}")

        step(f"Unsubscribe from {PV_NAME}")
        await ws.send(json.dumps({"action": "unsubscribe", "pv_names": [PV_NAME]}))
        await recv_until(ws, lambda m: is_event(m, "unsubscribed"),
                         SUBSCRIBE_TIMEOUT, "unsubscribed ack")
        ok("unsubscribed ack received")

        step("Close")
    ok("pv-socket closed cleanly")


async def exercise_device_socket() -> None:
    print(f"\n{B}─── device-socket ───{X}  url={DEVICE_WS_URL}  device={DEVICE_NAME}")

    step("Connect")
    async with websockets.connect(DEVICE_WS_URL) as ws:
        ok(f"connected to {DEVICE_WS_URL}")

        step("Ping → pong")
        await ws.send(json.dumps({"action": "ping"}))
        await recv_until(ws, lambda m: is_event(m, "pong"),
                         SUBSCRIBE_TIMEOUT, "pong")
        ok("pong received")

        step(f"Subscribe to device {DEVICE_NAME}")
        await ws.send(json.dumps({"action": "subscribe", "device": DEVICE_NAME}))
        ack = await recv_until(ws, lambda m: is_event(m, "subscribed"),
                               SUBSCRIBE_TIMEOUT, "subscribed ack")
        if ack.get("device") != DEVICE_NAME:
            fail(f"subscribed ack device mismatch: {ack}")
        ok(f"subscribed ack device={ack['device']}")

        # device-socket emits {event_type: "device_update", device, signal, value, ...}
        # — one frame per sub-PV change. _send_current_values fires immediately on
        # subscribe with the current value of every component.
        step(f"Receive at least one device_update for {DEVICE_NAME}")
        update = await recv_until(ws, lambda m: is_device_update(m, DEVICE_NAME),
                                  UPDATE_TIMEOUT, f"device_update for {DEVICE_NAME}")
        for required in ("signal", "value", "timestamp", "connected"):
            if required not in update:
                fail(f"device_update missing field {required!r}: {update}")
        ok(f"update received  signal={update['signal']!r}  value={update['value']!r}")

        step(f"Unsubscribe from device {DEVICE_NAME}")
        await ws.send(json.dumps({"action": "unsubscribe", "device": DEVICE_NAME}))
        await recv_until(ws, lambda m: is_event(m, "unsubscribed"),
                         SUBSCRIBE_TIMEOUT, "unsubscribed ack")
        ok("unsubscribed ack received")

        step("Close")
    ok("device-socket closed cleanly")


async def run() -> None:
    print(f"{B}direct_control WS exerciser{X}  host={DIRECT_HOST}")
    await exercise_pv_socket()
    await exercise_device_socket()
    print(f"\n{G}{B}direct_control WS: ALL CHECKS PASSED{X}")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print(f"{Y}interrupted{X}", file=sys.stderr)
        sys.exit(130)
    except websockets.WebSocketException as e:
        fail(f"WebSocket error: {e}")
    except Exception as e:  # noqa: BLE001
        fail(f"unexpected error: {type(e).__name__}: {e}")
