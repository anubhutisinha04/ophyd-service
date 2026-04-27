#!/usr/bin/env python3
"""
Exercise the direct_control_service WebSocket surface.

Companion to direct_control.sh, which deliberately skipped WebSockets
because bash + curl can't speak WS cleanly. This script does what curl
can't: connect to the PV-monitoring socket, subscribe to a live PV,
receive at least one update, unsubscribe, and close cleanly.

Endpoint and protocol (from
backend/direct_control_service/src/direct_control/monitoring/websocket_manager.py):

  ws://<host>:8003/api/v1/pv-socket

  Client → Server  (JSON):
      {"action": "subscribe",   "pv_names": ["..."]}
      {"action": "unsubscribe", "pv_names": ["..."]}
      {"action": "ping"}

  Server → Client (JSON):
      Events use the {"type": ..., "timestamp": ..., ...} envelope:
          {"type": "subscribed",   "pv_names": [...], ...}
          {"type": "unsubscribed", "pv_names": [...], ...}
          {"type": "pong", ...}
          {"type": "heartbeat", ...}      ← server-initiated, ignore
          {"type": "error", "message": "...", ...}
      PV updates use a separate envelope:
          {"event_type": "pv_update", "pv_name": "...", "value": ..., ...}

Dependency:
    pip install websockets   (>=12)
or  uv run --with websockets integration/exercise/direct_control_ws.py

Usage:
    ./direct_control_ws.py
    DIRECT_WS_URL=ws://remote:8003/api/v1/pv-socket ./direct_control_ws.py
    PV_NAME=random_walk:x ./direct_control_ws.py    # override default

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


WS_URL = os.environ.get("DIRECT_WS_URL", "ws://localhost:8003/api/v1/pv-socket")
PV_NAME = os.environ.get("PV_NAME", "random_walk:x")  # ticks frequently
SUBSCRIBE_TIMEOUT = float(os.environ.get("SUBSCRIBE_TIMEOUT", "5"))
UPDATE_TIMEOUT = float(os.environ.get("UPDATE_TIMEOUT", "8"))


# ANSI colors when stdout is a tty
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


async def recv_until(
    ws: Any,
    predicate,
    timeout: float,
    description: str,
) -> dict:
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


def is_pv_update(msg: dict, pv_name: str) -> bool:
    return msg.get("event_type") == "pv_update" and msg.get("pv_name") == pv_name


async def run() -> None:
    print(f"{B}direct_control WS exerciser{X}  target={WS_URL}  pv={PV_NAME}")

    step("Connect")
    async with websockets.connect(WS_URL) as ws:
        ok(f"connected to {WS_URL}")

        # ─── Ping/pong sanity ────────────────────────────────────────────
        step("Ping → pong")
        await ws.send(json.dumps({"action": "ping"}))
        await recv_until(
            ws,
            lambda m: is_event(m, "pong"),
            timeout=SUBSCRIBE_TIMEOUT,
            description="pong",
        )
        ok("pong received")

        # ─── Subscribe ──────────────────────────────────────────────────
        step(f"Subscribe to {PV_NAME}")
        await ws.send(json.dumps({"action": "subscribe", "pv_names": [PV_NAME]}))
        ack = await recv_until(
            ws,
            lambda m: is_event(m, "subscribed"),
            timeout=SUBSCRIBE_TIMEOUT,
            description="subscribed ack",
        )
        sub_pvs = ack.get("pv_names", [])
        if PV_NAME not in sub_pvs:
            fail(f"subscribed ack missing {PV_NAME!r}: {ack}")
        ok(f"subscribed ack pv_names={sub_pvs}")

        # ─── First update ───────────────────────────────────────────────
        step(f"Receive at least one pv_update for {PV_NAME}")
        update = await recv_until(
            ws,
            lambda m: is_pv_update(m, PV_NAME),
            timeout=UPDATE_TIMEOUT,
            description=f"pv_update for {PV_NAME}",
        )
        for required in ("value", "timestamp", "connected"):
            if required not in update:
                fail(f"pv_update missing required field {required!r}: {update}")
        ok(f"update received  value={update['value']!r}  connected={update['connected']}")

        # ─── Unsubscribe ────────────────────────────────────────────────
        step(f"Unsubscribe from {PV_NAME}")
        await ws.send(json.dumps({"action": "unsubscribe", "pv_names": [PV_NAME]}))
        await recv_until(
            ws,
            lambda m: is_event(m, "unsubscribed"),
            timeout=SUBSCRIBE_TIMEOUT,
            description="unsubscribed ack",
        )
        ok("unsubscribed ack received")

        # ─── Close cleanly ──────────────────────────────────────────────
        step("Close")
    ok("connection closed cleanly")

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
