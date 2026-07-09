"""
Pydantic models for Direct Device Control + Monitoring Service.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer


def _serialize_unix_epoch(value: datetime) -> float:
    """Serialize a datetime as unix epoch seconds for finch's WS contract.

    finch does numeric arithmetic on ``timestamp`` (e.g.
    ``TableDeviceController.tsx`` flash-row check). ISO strings would
    silently coerce to NaN there. Shared by ``PVUpdate`` and
    ``DeviceUpdate``.
    """
    return value.timestamp()


# ===== Device Control Enums =====


class DeviceLockStatus(str, Enum):
    """Status of a device's command-availability gate.

    Three blocking states (DISABLED, LOCKED, UNKNOWN) all mean "don't
    command"; AVAILABLE is the only state that allows commands. Monitoring
    (read / WS subscribe) is unaffected by this enum — the registry
    validation gate handles that separately.
    """

    AVAILABLE = "available"
    LOCKED = (
        "locked"  # held by an active plan (lock written by queueserver to configuration_service)
    )
    DISABLED = "disabled"  # administratively disabled in configuration_service
    UNKNOWN = "unknown"


class CommandMode(str, Enum):
    """Command execution mode for PV writes."""

    PUT_COMPLETION = "put-completion"
    FIRE_AND_FORGET = "fire-and-forget"


class SubscriptionStatus(str, Enum):
    """Status of a PV subscription."""

    ACTIVE = "active"
    PAUSED = "paused"
    ERROR = "error"
    DISCONNECTED = "disconnected"


class AlarmSeverity(str, Enum):
    """EPICS alarm severity levels."""

    NO_ALARM = "NO_ALARM"
    MINOR = "MINOR"
    MAJOR = "MAJOR"
    INVALID = "INVALID"


ALARM_SEVERITY_NAMES = {
    0: "NO_ALARM",
    1: "MINOR",
    2: "MAJOR",
    3: "INVALID",
}

# EPICS alarm status codes (menuAlarmStat) → name. Stable across EPICS Base
# versions; used to render PVUpdate.alarm_status from the integer STAT the CA
# monitor carries.
ALARM_STATUS_NAMES = {
    0: "NO_ALARM",
    1: "READ",
    2: "WRITE",
    3: "HIHI",
    4: "HIGH",
    5: "LOLO",
    6: "LOW",
    7: "STATE",
    8: "COS",
    9: "COMM",
    10: "TIMEOUT",
    11: "HWLIMIT",
    12: "CALC",
    13: "SCAN",
    14: "LINK",
    15: "SOFT",
    16: "BAD_SUB",
    17: "UDF",
    18: "DISABLE",
    19: "SIMM",
    20: "READ_ACCESS",
    21: "WRITE_ACCESS",
}


# ===== Device Control Request/Response =====


class PVSetRequest(BaseModel):
    """
    Request to set a PV value (Low Fidelity Channel).

    Completion modes:
    - wait=False, use_complete=False (default): fire-and-forget — issue write, return.
    - wait=True,  use_complete=False: block a CA thread until put finishes.
    - use_complete=True: put-with-callback — CA thread is freed; service polls
      for completion via the pyepics put-callback mechanism. Preferred for
      long puts over HTTP because no worker thread is held.

    `connection_timeout` bounds how long we wait to establish CA connection;
    separate from `timeout` which bounds the put itself.

    `ftype` forces a non-native DBR type on the wire (rare, e.g. when an IOC
    expects CHAR waveforms represented differently). Leave None for native.
    """

    model_config = ConfigDict(extra="forbid")

    pv_name: str = Field(..., description="EPICS PV name")
    value: Any = Field(..., description="Value to set")
    wait: bool = Field(False, description="Block the CA thread until put completion")
    timeout: float | None = Field(
        None,
        description="Put timeout in seconds (used with wait=True or use_complete=True)",
        ge=0.0,
    )
    connection_timeout: float | None = Field(
        None, description="Max seconds to wait for CA connection (pyepics default 5s)", ge=0.0
    )
    use_complete: bool = Field(
        False,
        description=(
            "If True, wait for put via pyepics put-callback instead of blocking a "
            "CA thread. Overrides `wait` (always waits) but frees the worker."
        ),
    )
    ftype: int | None = Field(
        None, description="Force non-native DBR type (power-user knob; leave null for native)"
    )
    check_limits: bool | None = Field(
        None,
        description=(
            "Per-request override for the ctrl-limit gate. None (default) uses "
            "Settings.check_ctrl_limits. Explicitly False bypasses the check "
            "even when the global setting is on — the escape hatch for values "
            "known to be safe but outside the IOC-advertised range (e.g. an "
            "operator override, or writing a raw byte to a record whose LOPR/"
            "HOPR are miscalibrated). True forces the check on."
        ),
    )


class PVSetResponse(BaseModel):
    """Response from PV set operation."""

    model_config = ConfigDict(extra="forbid")

    pv_name: str
    success: bool
    value_set: Any
    timestamp: datetime
    coordination_checked: bool
    mode: CommandMode
    message: str | None = None


class PVSetBatchRequest(BaseModel):
    """Sequence of PV writes applied with fail-hard semantics.

    Used for "configure beamline for edge X" flows where a partially-applied
    preset is worse than an explicit failure. The service runs caputs in the
    order given; on the first failure it stops and does *not* attempt the
    rest. Successful items remain applied — the IOC won't roll them back.

    ``max_length`` is a soft sanity guard; a typical edge preset is ~15
    caputs. Raise it if you have a real use case for larger batches.
    """

    model_config = ConfigDict(extra="forbid")

    caputs: list[PVSetRequest] = Field(
        ...,
        min_length=1,
        max_length=100,
        description="PV writes to apply in order. Stops on first failure.",
    )


class PVSetBatchItemResult(BaseModel):
    """Per-item outcome from a batch caput.

    Carries enough detail that a caller can recover the same diagnostic
    they'd see from a single ``POST /api/v1/pv/set`` (status code + message)
    while still being part of a JSON list. ``status_code`` is the HTTP code
    the equivalent single-item call would have returned
    (200 / 404 / 409 / 423 / 503 / 500) — direct_control returns the batch
    envelope itself with 200 so the caller can read the full ``results``
    list.
    """

    model_config = ConfigDict(extra="forbid")

    pv_name: str
    success: bool
    value_set: Any = None
    timestamp: datetime
    coordination_checked: bool = False
    mode: CommandMode | None = None
    message: str | None = None
    error_type: str | None = Field(
        None, description="Exception class name when success=false (e.g. RegistryValidationError)"
    )
    status_code: int | None = Field(
        None,
        description="HTTP status the equivalent single /pv/set call would have returned",
    )


class PVSetBatchResponse(BaseModel):
    """Aggregate response for a batch caput.

    ``ok=true`` iff every item succeeded. ``applied`` is the count of items
    that were applied before a halt (or the full batch on success).
    ``results`` always contains exactly the items that were attempted —
    items past the failure point are absent, so ``requested - len(results)``
    is the count of items that were skipped after the halt.
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    applied: int
    requested: int
    results: list[PVSetBatchItemResult]


class EnrichmentSpec(BaseModel):
    """One device-class + sub-path to enrich.

    Sent by configuration_service to direct_control when the
    configuration_service-side static resolver returns
    ``needs_enrichment`` (typically an ophyd ``FormattedComponent`` with
    runtime placeholders like ``{self.parent.prefix}``). Direct-control
    instantiates the device class once via its ophyd-cache, walks the
    sub-path with ``operator.attrgetter``, and reports the underlying PV.

    ``sub_path`` is the dotted chain *after* the device name (since
    direct-control has no registry of its own). For an address like
    ``m1a.pit.actuate``, the head segment ``m1a`` is consumed by
    configuration_service for the device lookup; direct-control receives
    ``sub_path="pit.actuate"`` plus the class path and prefix.
    """

    model_config = ConfigDict(extra="forbid")

    device_class_path: str = Field(
        ..., description="Fully qualified import path of the device class."
    )
    prefix: str = Field(..., description="EPICS prefix the device is constructed with.")
    sub_path: str = Field(
        ...,
        description=(
            "Dotted attribute chain to walk on the instantiated device. "
            "Empty string means the device IS the leaf signal."
        ),
    )


class EnrichmentRequest(BaseModel):
    """Batch enrichment request from configuration_service."""

    model_config = ConfigDict(extra="forbid")

    items: list[EnrichmentSpec] = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Specs to enrich, in caller order. Results match by index.",
    )


class EnrichmentResultItem(BaseModel):
    """Per-item enrichment outcome.

    Results are returned in the same order as the request items so the
    caller (configuration_service) can correlate by index — no
    passthrough identifier needed.
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    pv_name: str | None = None
    error_type: str | None = Field(
        None,
        description="Short tag identifying the failure category when ok=false.",
    )
    message: str | None = None


class EnrichmentResponse(BaseModel):
    """Aggregate enrichment response.

    Per-item results in caller order. Resolution is read-only and never
    halts — one bad item doesn't fail the others.
    """

    model_config = ConfigDict(extra="forbid")

    results: list[EnrichmentResultItem]


class DeviceCommandRequest(BaseModel):
    """
    Request to execute a device method (High Fidelity Channel).

    use_put=False (default): ophyd set() waits for completion.
    use_put=True: ophyd put() returns immediately.
    """

    model_config = ConfigDict(extra="forbid")

    device_name: str
    method: str
    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)
    timeout: float | None = Field(None, ge=0.0)
    use_put: bool = False


class DeviceCommandResponse(BaseModel):
    """Response from device command execution."""

    model_config = ConfigDict(extra="forbid")

    device_name: str
    method: str
    success: bool
    result: Any = None
    timestamp: datetime
    coordination_checked: bool
    message: str | None = None
    use_put: bool = False


class InstantiationSpec(BaseModel):
    """How to construct a live device object for device-level control.

    Sourced from configuration_service's ``DeviceInstantiationSpec``
    (``GET /api/v1/devices/{name}/instantiation``) or from the optional
    ``device_class``/``args``/``kwargs``/``framework`` fields of a file-
    registry device entry. ``framework`` is an advisory tag
    ("ophyd-sync" | "ophyd-async"); the authoritative classification is
    ``drivers.detect_framework`` on the imported class, and a mismatching
    tag is a hard error, never silently overridden.
    """

    model_config = ConfigDict(extra="ignore")

    name: str
    device_class: str = Field(
        ..., description="Fully qualified class path, e.g. 'ophyd.EpicsMotor'"
    )
    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)
    active: bool = True
    framework: str | None = Field(
        None, description="Advisory framework tag: 'ophyd-sync' | 'ophyd-async'"
    )


class CoordinationStatus(BaseModel):
    """Coordination status read from configuration_service's device-lock state.

    Locks are written into configuration_service by queueserver / any plan-
    execution service via ``POST /api/v1/devices/lock``; direct_control reads
    them via ``GET /api/v1/devices/{name}/status``. We never talk to EE or
    queueserver directly.
    """

    model_config = ConfigDict(extra="forbid")

    device_available: bool
    locked_by: str | None = None
    status: DeviceLockStatus
    timestamp: datetime


# ===== PV Metadata / Value Models =====


class PVValue(BaseModel):
    """
    Current value of a PV (as-ophyd-api compatible).

    Returned by PVMonitor.get_value(). Includes EPICS metadata for richer
    client display (units, precision, limits, alarm status) plus array shape
    metadata derived from the raw numpy return before conversion. `value`
    itself is JSON-friendly (scalars and nested lists); clients that want
    raw binary use the endpoint's `Accept: application/octet-stream` mode.
    """

    model_config = ConfigDict(extra="allow")

    pv_name: str
    value: Any
    timestamp: datetime
    status: int = 0
    severity: int = 0
    connected: bool = True

    # Array structure captured pre-conversion (all zero/None for scalars).
    shape: list[int] = Field(default_factory=list)
    dtype: str | None = None
    ndim: int = 0
    nbytes: int = 0

    units: str | None = None
    precision: int | None = None
    enum_strs: list[str] | None = None

    lower_ctrl_limit: float | None = None
    upper_ctrl_limit: float | None = None
    lower_disp_limit: float | None = None
    upper_disp_limit: float | None = None

    # Default to no access — assume locked-out until EPICS confirms otherwise.
    # Pre-M14 these defaulted to True/True so any construction site that
    # forgot to populate them would advertise the PV as fully writable.
    read_access: bool = False
    write_access: bool = False


class PVUpdate(BaseModel):
    """PV update notification sent via WebSocket (ophyd-websocket compatible).

    Wire field names match finch's ``ValueUpdateResponse`` directly — `pv`
    (not `pv_name`) and `timestamp` as unix-epoch seconds (not ISO string).
    See ``finch/src/api/ophyd/ophydPVSocketTypes.ts`` for the contract.
    """

    model_config = ConfigDict(extra="forbid")

    event_type: str = "pv_update"
    pv: str
    value: Any
    timestamp: datetime
    status: int = 0
    severity: int = 0
    connected: bool = True
    # Default to no access. See PVValue rationale; PVUpdate's pre-M14
    # default of read_access=True (write_access already False) silently
    # painted streaming-update PVs as readable regardless of CA reality.
    read_access: bool = False
    write_access: bool = False
    alarm_status: str | None = None
    alarm_severity: int | None = None
    alarm_severity_name: str | None = None
    lower_ctrl_limit: float | None = None
    upper_ctrl_limit: float | None = None
    lower_disp_limit: float | None = None
    upper_disp_limit: float | None = None
    units: str | None = None
    precision: int | None = None

    _serialize_timestamp = field_serializer("timestamp")(_serialize_unix_epoch)

    @classmethod
    def from_value(cls, pv_value: "PVValue", **overrides: Any) -> "PVUpdate":
        """Build a PVUpdate carrying the core fields of a PVValue (plus overrides)."""
        return cls(
            pv=pv_value.pv_name,
            value=pv_value.value,
            timestamp=pv_value.timestamp,
            status=pv_value.status,
            severity=pv_value.severity,
            # Derive the friendly alarm fields from the raw ints so the
            # initial-subscribe snapshot carries the same alarm info the
            # streaming updates do (not just status/severity integers).
            alarm_severity=pv_value.severity,
            alarm_severity_name=ALARM_SEVERITY_NAMES.get(pv_value.severity),
            alarm_status=ALARM_STATUS_NAMES.get(pv_value.status),
            connected=pv_value.connected,
            units=pv_value.units,
            precision=pv_value.precision,
            lower_ctrl_limit=pv_value.lower_ctrl_limit,
            upper_ctrl_limit=pv_value.upper_ctrl_limit,
            lower_disp_limit=pv_value.lower_disp_limit,
            upper_disp_limit=pv_value.upper_disp_limit,
            read_access=pv_value.read_access,
            write_access=pv_value.write_access,
            **overrides,
        )


class PVInfo(BaseModel):
    """Detailed PV information including metadata."""

    model_config = ConfigDict(extra="forbid")

    pv_name: str
    value: Any = None
    connected: bool
    # Default to no access (mirror PVValue/PVUpdate post-M14).
    read_access: bool = False
    write_access: bool = False
    timestamp: datetime

    lower_ctrl_limit: float | None = None
    upper_ctrl_limit: float | None = None
    lower_disp_limit: float | None = None
    upper_disp_limit: float | None = None

    units: str | None = None
    precision: int | None = None
    enum_strs: list[str] | None = None

    alarm_status: str | None = None
    alarm_severity: AlarmSeverity | None = None


class PVValueResponse(BaseModel):
    """PV value response with connection and access info."""

    model_config = ConfigDict(extra="forbid")

    pv_name: str
    value: Any
    timestamp: datetime
    connected: bool = True
    # Default to no access (mirror PVValue/PVUpdate post-M14).
    read_access: bool = False
    write_access: bool = False


class PVLimits(BaseModel):
    """PV value limits for validation."""

    model_config = ConfigDict(extra="forbid")

    pv_name: str
    lower_limit: float | None = None
    upper_limit: float | None = None
    has_limits: bool = False


# ===== Monitoring Subscription Models =====


class PVMonitorRequest(BaseModel):
    """Request to monitor one or more PVs."""

    model_config = ConfigDict(extra="forbid")

    pv_names: list[str]
    update_rate: float | None = Field(None, ge=0.0)
    buffer_size: int | None = Field(None, ge=1, le=1000)


class PVSubscription(BaseModel):
    """Information about an active PV subscription."""

    model_config = ConfigDict(extra="forbid")

    subscription_id: str
    pv_names: list[str]
    status: SubscriptionStatus
    created_at: datetime
    last_update: datetime | None = None
    update_count: int = 0
    client_id: str | None = None


# ===== WebSocket Models (ophyd-websocket compatible) =====


class WebSocketAction(str, Enum):
    """WebSocket control actions (ophyd-websocket compatible)."""

    SET = "set"
    GET = "get"
    PING = "ping"
    SUBSCRIBE = "subscribe"
    UNSUBSCRIBE = "unsubscribe"
    SUBSCRIBE_SAFELY = "subscribeSafely"
    SUBSCRIBE_READ_ONLY = "subscribeReadOnly"
    REFRESH = "refresh"
    STOP = "stop"


class WebSocketMessage(BaseModel):
    """Incoming WebSocket message."""

    model_config = ConfigDict(extra="allow")

    action: WebSocketAction
    pv: str | None = None
    pv_names: list[str] | None = None
    device: str | None = None
    component: str | None = None
    value: Any | None = None
    timeout: float | None = None


class WebSocketSetRequest(BaseModel):
    """WebSocket set request."""

    model_config = ConfigDict(extra="forbid")

    action: WebSocketAction
    pv: str | None = None
    device: str | None = None
    component: str | None = None
    value: Any | None = None
    timeout: float | None = None


class WebSocketSetResponse(BaseModel):
    """WebSocket set response."""

    model_config = ConfigDict(extra="forbid")

    type: str
    pv: str | None = None
    device: str | None = None
    component: str | None = None
    value: Any | None = None
    success: bool
    message: str | None = None
    timestamp: str


# ===== Nested Component Models =====


class NestedDeviceRequest(BaseModel):
    """Request to access nested device component."""

    model_config = ConfigDict(extra="forbid")

    method: str = "read"
    value: Any | None = None
    timeout: float | None = None


class NestedDeviceResponse(BaseModel):
    """Response from nested device access."""

    model_config = ConfigDict(extra="forbid")

    device_path: str
    method: str
    success: bool
    result: Any = None
    timestamp: datetime
    message: str | None = None


# ===== Device-Socket Models =====


class DeviceUpdate(BaseModel):
    """Device value update notification (ophyd-websocket compatible).

    Wire shape matches finch's ``ValueUpdateResponse`` for the device
    socket — `device` (not `device_name`), `timestamp` as unix-epoch
    seconds. See ``finch/src/api/ophyd/ophydDeviceSocketTypes.ts``.
    """

    model_config = ConfigDict(extra="forbid")

    event_type: str = "device_update"
    device: str
    signal: str | None = None
    value: Any
    timestamp: datetime
    connected: bool = True
    read_access: bool | None = True
    write_access: bool | None = None

    _serialize_timestamp = field_serializer("timestamp")(_serialize_unix_epoch)


class DeviceInfo(BaseModel):
    """Device information from configuration service."""

    model_config = ConfigDict(extra="allow")

    name: str
    device_type: str
    ophyd_class: str | None = None
    pvs: dict[str, str] = Field(default_factory=dict)
    is_movable: bool = False
    is_readable: bool = True


# ===== Stop Operation Models =====


class StopRequest(BaseModel):
    """Request to stop a device/motor."""

    model_config = ConfigDict(extra="forbid")

    timeout: float | None = None


class StopResponse(BaseModel):
    """Response from stop operation."""

    model_config = ConfigDict(extra="forbid")

    pv_name: str
    success: bool
    timestamp: datetime
    message: str | None = None


# ===== Health Response =====


class HealthResponse(BaseModel):
    """Health check response for the merged service."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["healthy", "unhealthy"] = "healthy"
    timestamp: datetime
    coordination_service_available: bool
    coordination_service_detail: str | None = None
    # Running mode, so a file-backed / read-only deployment is always visible.
    registry_backend: str = "http"  # http | file (auto resolves to one of these)
    read_only: bool = False
    active_subscriptions: int = 0
    connected_pvs: int = 0
    websocket_connections: int = 0


class ServiceAvailability(BaseModel):
    """Result of a dependency availability probe.

    `detail` is populated only when ``available=False`` so the caller can
    surface the actual failure reason in /health, instead of the bare
    True/False that hid all the failure modes pre-S6.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    available: bool
    detail: str | None = None


# ===== Exceptions =====


class ControlError(Exception):
    """Base exception for control errors."""


class DeviceLockedError(ControlError):
    """Raised when device is locked by active plan."""


class DeviceDisabledError(ControlError):
    """Raised when device is administratively disabled in configuration_service."""


class DeviceUnavailableError(ControlError):
    """Raised when a device's coordination status is neither AVAILABLE,
    LOCKED, nor DISABLED — e.g. UNKNOWN, where configuration_service returned
    a state we don't model. The command is refused (maps to HTTP 409), but
    this is an orchestration/coordination policy outcome, NOT a PV-health or
    EPICS execution failure, so it must never be reported as PV health."""


class CoordinationCheckError(ControlError):
    """Raised when coordination check fails."""


class MethodNotAllowedError(ControlError):
    """Raised when a device method is outside the allowlist or the target
    object doesn't implement it. Maps to HTTP 400."""


class DeviceNotInstantiableError(ControlError):
    """Raised when device-level control is requested for a device whose
    registry entry carries no instantiation spec (class path + ctor args),
    or whose spec is marked inactive. Maps to HTTP 422."""


class ComponentNotFoundError(ControlError):
    """Raised when a nested component path doesn't exist on the live device
    (e.g. ``motor1.no_such_signal``). Maps to HTTP 404."""


class ValueLimitError(ControlError):
    """Raised when a numeric PV write would land outside the IOC-advertised
    control limits (``lower_ctrl_limit`` / ``upper_ctrl_limit``).

    The write is refused *before* it reaches EPICS — the value never lands
    on the IOC. Maps to HTTP 422 (well-formed request, rejected by the
    ctrl-limit safety gate) and is NOT reported as a PV-health failure
    (limit-guard rejections reflect operator input, not IOC health).

    Skipped when the target PV has no advertised limits (records without
    LOPR/HOPR, or ``lower_ctrl_limit == upper_ctrl_limit == 0`` which is
    EPICS's "no limits enforced" convention), for non-numeric values, and
    when the caller opts out via ``PVSetRequest.check_limits=False`` or the
    ``check_ctrl_limits=False`` service setting.
    """


class MonitoringError(Exception):
    """Base exception for monitoring errors."""


class PVNotFoundError(MonitoringError):
    """Raised when a requested PV cannot be found."""


class PVReadError(MonitoringError):
    """Raised when reading a subscribed PV fails (transient EPICS error).

    Distinct from ``PVNotFoundError`` so callers can map it to a 5xx
    instead of a 404 — the PV *is* tracked, EPICS just failed to read.
    """


class SubscriptionError(MonitoringError):
    """Raised when subscription management fails."""
