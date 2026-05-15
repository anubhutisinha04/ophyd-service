#!/usr/bin/env python3
"""Bulk-register the IOS happi DB into a running configuration_service.

Assumes the service is already up with --load-strategy empty (or any
strategy that left the registry empty). For each entry in
happi_db.json, POSTs to /api/v1/devices with the metadata +
instantiation_spec shape the API expects.

Usage:
    python load_via_api.py                            # default localhost:8004
    CONFIG_URL=http://host:8004 python load_via_api.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

CONFIG_URL = os.environ.get("CONFIG_URL", "http://localhost:8004").rstrip("/")
HAPPI_PATH = Path(__file__).parent / "happi_db.json"

# device_class short-name → DeviceLabel enum value
LABEL_BY_CLASS = {
    "EpicsMotor": "motor",
    "EpicsSignal": "signal",
    "EpicsSignalRO": "signal",
    "SpecsDetector": "detector",
    "Xspress3IOS": "detector",
    "DodgyEpicsScaler": "detector",
    "Vortex": "detector",
}


def render_kwargs(template_kwargs: dict, name: str) -> dict:
    """Render happi {{name}} placeholders; drop unrenderable templates."""
    out: dict = {}
    for k, v in template_kwargs.items():
        if k == "name":
            out[k] = name
        elif isinstance(v, str) and "{{" in v:
            continue
        else:
            out[k] = v
    return out


def build_body(name: str, entry: dict) -> dict:
    device_class = entry["device_class"]
    short_class = device_class.split(".")[-1]
    prefix = entry["prefix"]
    doc = entry.get("documentation") or ""

    return {
        "metadata": {
            "name": name,
            "device_label": LABEL_BY_CLASS.get(short_class, "device"),
            "ophyd_class": short_class,
            "is_readable": True,
            "pvs": {"prefix": prefix},
            "documentation": doc,
        },
        "instantiation_spec": {
            "name": name,
            "device_class": device_class,
            "args": [prefix],
            "kwargs": render_kwargs(entry.get("kwargs", {}), name),
            "active": True,
        },
    }


def post(name: str, body: dict) -> tuple[str, int, str]:
    req = urllib.request.Request(
        f"{CONFIG_URL}/api/v1/devices",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return name, r.status, ""
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode()[:200]
        except Exception:
            detail = ""
        return name, e.code, detail
    except Exception as e:
        return name, 0, str(e)[:200]


def main() -> int:
    happi = json.loads(HAPPI_PATH.read_text())
    print(f"Loading {len(happi)} devices into {CONFIG_URL}\n")

    added = conflicts = errors = 0
    for name, entry in happi.items():
        body = build_body(name, entry)
        n, status, detail = post(name, body)
        if 200 <= status < 300:
            print(f"✓ {n:30s}  HTTP {status}")
            added += 1
        elif status == 409:
            print(f"= {n:30s}  already exists")
            conflicts += 1
        else:
            print(f"✗ {n:30s}  HTTP {status}  {detail}")
            errors += 1

    print(f"\nDone: {added} added, {conflicts} already present, {errors} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
