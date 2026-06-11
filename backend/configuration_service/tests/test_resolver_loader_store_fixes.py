"""Regressions for a cluster of config-service correctness fixes.

- path_resolver: an address ending on a DynamicDeviceComponent resolved
        to the bare parent prefix (a caller would caput a nonexistent PV).
- BITS loader: a class-path module key without an explicit creator
        produced an unimportable device_class ("ophyd.EpicsMotor.m1").
- SQLite: ping()'s busy_timeout PRAGMA leaked into the pooled
        connection, permanently dropping its write-lock wait from 30s to 2s.
- direct_control_client: a 200 enrich response whose rows lack "ok"
        escaped as KeyError instead of degrading to DirectControlUnavailable.
- /devices/history: a NUL byte in the device_name filter reached the
        SQL driver (500 on PostgreSQL); now rejected with 422.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from configuration_service.config import Settings
from configuration_service.main import create_app
from configuration_service.models import DeviceRegistry
from configuration_service.path_resolver import Outcome, _walk_class


@pytest.fixture
def client(db_url):
    settings = Settings(
        use_mock_data=True,
        database_url=db_url,
        device_change_history_enabled=True,
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


# ===== Trailing DynamicDeviceComponent is not a resolvable leaf ==============


def test_walk_class_rejects_trailing_ddc():
    from ophyd import EpicsScaler

    outcome, _path, bad = _walk_class(EpicsScaler, ["channels"], "BL:DET1:")
    assert outcome is Outcome.NO_SUCH_ATTR
    assert bad == "channels"


def test_walk_class_still_resolves_real_leaves():
    from ophyd import EpicsMotor

    outcome, pv, _ = _walk_class(EpicsMotor, ["user_readback"], "BL:M1")
    assert outcome is Outcome.RESOLVED
    assert pv == "BL:M1.RBV"


# ===== BITS class-path keys produce importable specs =========================


def _bits_process(module_path: str, entry: dict, name: str = "m1"):
    from configuration_service.loader import BitsProfileLoader

    registry = DeviceRegistry()
    loader = BitsProfileLoader.__new__(BitsProfileLoader)
    loader._process_entry(name, entry, module_path, None, registry)
    return registry.instantiation_specs[name]


def test_bits_class_path_key_without_creator_is_importable():
    spec = _bits_process("ophyd.EpicsMotor", {"prefix": "BL:M1"})
    assert spec.device_class == "ophyd.EpicsMotor"


def test_bits_module_key_still_appends_creator():
    spec = _bits_process("ophyd.sim", {"creator": "motor", "prefix": "BL:M2"})
    assert spec.device_class == "ophyd.sim.motor"


# ===== ping() must not leak the short busy_timeout into the pool =============


def test_sqlite_ping_restores_pooled_busy_timeout(tmp_path):
    from configuration_service.db import make_engine
    from configuration_service.device_registry_store import DeviceRegistryStore

    engine = make_engine(f"sqlite+pysqlite:///{tmp_path / 'ping.db'}")
    try:
        store = DeviceRegistryStore(engine)
        store.initialize()

        with engine.connect() as conn:
            baseline = conn.exec_driver_sql("PRAGMA busy_timeout").scalar()

        store.ping()

        # The pooled connection must come back with the engine-configured
        # timeout, not the probe's 2000ms (pre-fix it stayed at 2000).
        with engine.connect() as conn:
            after = conn.exec_driver_sql("PRAGMA busy_timeout").scalar()
        assert after == baseline
        assert after != 2000
    finally:
        engine.dispose()


# ===== Malformed enrich rows degrade, never KeyError =========================


async def test_enrich_row_missing_ok_degrades_to_unavailable():
    from configuration_service.direct_control_client import (
        DirectControlClient,
        DirectControlUnavailable,
        EnrichmentSpec,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"pv_name": "X:PV"}]})

    client = DirectControlClient("http://stub")
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://stub"
    )
    try:
        with pytest.raises(DirectControlUnavailable, match="malformed result row"):
            await client.enrich(
                [EnrichmentSpec(device_class_path="a.B", prefix="X:", sub_path="v")]
            )
    finally:
        await client.aclose()


# ===== NUL bytes in history filter are rejected, not 500 =====================


def test_history_rejects_nul_in_device_name(client):
    resp = client.get("/api/v1/devices/history", params={"device_name": "a\x00b"})
    assert resp.status_code == 422
    assert "NUL" in resp.json()["detail"]

    # A clean filter still works.
    resp = client.get("/api/v1/devices/history", params={"device_name": "sample_x"})
    assert resp.status_code == 200
