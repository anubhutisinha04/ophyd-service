"""Contract test: queueserver's device payload ↔ configuration_service models.

Queueserver hand-builds the ``{name: {"metadata": ..., "spec": ...}}`` payload
(``device_introspection.build_config_service_payload``) and POSTs it to
configuration_service, which validates it against ``DeviceCreateRequest``
(``DeviceMetadata`` + ``DeviceInstantiationSpec``). Those two model definitions
live in separate repos-within-the-monorepo with no shared type, so they can
drift silently: ``DeviceMetadata`` uses ``extra="ignore"``, so a metadata key
the queueserver emits that config no longer models is dropped without error.

These tests pin the contract without a running server:
- every introspection payload validates as a ``DeviceCreateRequest``;
- the metadata keys the queueserver emits are all modeled by ``DeviceMetadata``
  (a new unmodeled key → silent field loss → this test fails);
- the spec keys are all modeled by ``DeviceInstantiationSpec``;
- the core spec fields round-trip unchanged.
"""

from __future__ import annotations

import pytest

pytest.importorskip("configuration_service")

from configuration_service.models import (  # noqa: E402
    DeviceCreateRequest,
    DeviceInstantiationSpec,
    DeviceMetadata,
)

from queueserver_service.manager.device_introspection import (  # noqa: E402
    build_config_service_payload,
)
from tests.manager.test_device_introspection import (  # noqa: E402
    FakeAsyncDevice,
    FakeOphydMotor,
    FakePositionalMotor,
    FakePrefixOnly,
)


def _sample_devices():
    """A diverse set of device shapes the introspection pipeline handles."""
    return {
        "m1": FakeOphydMotor(prefix="XF:01-Mtr{M1}", name="m1"),
        "d1": FakeAsyncDevice(prefix="XF:01-Det{D1}", name="d1"),
        "p1": FakePrefixOnly(prefix="XF:01-Pfx{P1}", name="p1"),
        "pm1": FakePositionalMotor("XF:01-Mtr{M2}", name="pm1"),
    }


def test_every_payload_validates_as_device_create_request():
    payload = build_config_service_payload(_sample_devices())
    assert set(payload) == {"m1", "d1", "p1", "pm1"}
    for name, entry in payload.items():
        # This mirrors exactly what config-service does on POST /api/v1/devices
        # (upsert_device wraps {metadata, instantiation_spec}).
        model = DeviceCreateRequest.model_validate(
            {"metadata": entry["metadata"], "instantiation_spec": entry["spec"]}
        )
        assert model.metadata.name == name
        assert model.instantiation_spec.name == name


def test_emitted_metadata_keys_are_all_modeled():
    """Guards against silent field loss: DeviceMetadata ignores extra keys, so a
    metadata key the queueserver emits that config no longer models would be
    dropped without error. Fail loudly instead."""
    modeled = set(DeviceMetadata.model_fields)
    payload = build_config_service_payload(_sample_devices())
    for name, entry in payload.items():
        emitted = set(entry["metadata"])
        unmodeled = emitted - modeled
        assert not unmodeled, (
            f"queueserver emits metadata key(s) {sorted(unmodeled)} for {name!r} "
            f"that configuration_service.DeviceMetadata does not model — these "
            f"are silently dropped on upsert (schema drift)."
        )


def test_emitted_spec_keys_are_all_modeled():
    modeled = set(DeviceInstantiationSpec.model_fields)
    payload = build_config_service_payload(_sample_devices())
    for name, entry in payload.items():
        unmodeled = set(entry["spec"]) - modeled
        assert not unmodeled, (
            f"queueserver emits spec key(s) {sorted(unmodeled)} for {name!r} that "
            f"configuration_service.DeviceInstantiationSpec does not model."
        )


def test_core_spec_fields_round_trip():
    payload = build_config_service_payload(_sample_devices())
    for entry in payload.values():
        spec_in = entry["spec"]
        spec_out = DeviceInstantiationSpec.model_validate(spec_in)
        assert spec_out.name == spec_in["name"]
        assert spec_out.device_class == spec_in["device_class"]
        assert spec_out.args == spec_in["args"]
        assert spec_out.kwargs == spec_in["kwargs"]
        assert spec_out.active == spec_in["active"]


def test_device_label_is_a_valid_enum_value():
    """The queueserver-inferred device_label must be a DeviceLabel the config
    model accepts — otherwise every upsert of that device shape 422s."""
    payload = build_config_service_payload(_sample_devices())
    for entry in payload.values():
        # model_validate coerces/validates the enum; an invalid label raises.
        md = DeviceMetadata.model_validate(entry["metadata"])
        assert md.device_label is not None
