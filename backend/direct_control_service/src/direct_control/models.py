"""
Pydantic models for Direct Device Control + Monitoring Service.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

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
    LOCKED = "locked"      # held by an active plan (lock written by queueserver to configuration_service)
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
    timeout: Optional[float] = Field(
        None,
        description="Put timeout in seconds (used with wait=True or use_complete=True)",
        ge=0.0,
    )
    connection_timeout: Optional[float] = Field(
        None, description="Max seconds to wait for CA connection (pyepics default 5s)", ge=0.0
    )
    use_complete: bool = Field(
        False,
        description=(
            "If True, wait for put via pyepics put-callback instead of blocking a "
            "CA thread. Overrides `wait` (always waits) but frees the worker."
        ),
    )
    ftype: Optional[int] = Field(
        None, description="Force non-native DBR type (power-user knob; leave null for native)"
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
    message: Optional[str] = None


class DeviceCommandRequest(BaseModel):
    """
    Request to execute a device method (High Fidelity Channel).

    use_put=False (default): ophyd set() waits for completion.
    use_put=True: ophyd put() returns immediately.
    """

    model_config = ConfigDict(extra="forbid")

    device_name: str
    method: str
    args: List[Any] = Field(default_factory=list)
    kwargs: Dict[str, Any] = Field(default_factory=dict)
    timeout: Optional[float] = Field(None, ge=0.0)
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
    message: Optional[str] = None
    use_put: bool = False


class CoordinationStatus(BaseModel):
    """Coordination status read from configuration_service's device-lock state.

    Locks are written into configuration_service by queueserver / any plan-
    execution service via ``POST /api/v1/devices/lock``; direct_control reads
    them via ``GET /api/v1/devices/{name}/status``. We never talk to EE or
    queueserver directly — see feedback_direct_control_no_ee_polling.
    """

    model_config = ConfigDict(extra="forbid")

    device_available: bool
    locked_by: Optional[str] = None
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
    shape: List[int] = Field(default_factory=list)
    dtype: Optional[str] = None
    ndim: int = 0
    nbytes: int = 0

    units: Optional[str] = None
    precision: Optional[int] = None
    enum_strs: Optional[List[str]] = None

    lower_ctrl_limit: Optional[float] = None
    upper_ctrl_limit: Optional[float] = None
    lower_disp_limit: Optional[float] = None
    upper_disp_limit: Optional[float] = None

    # Default to no access — assume locked-out until EPICS confirms otherwise.
    # Pre-M14 these defaulted to True/True so any construction site that
    # forgot to populate them would advertise the PV as fully writable.
    # See feedback_no_silent_fallbacks.
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
    alarm_status: Optional[str] = None
    alarm_severity: Optional[int] = None
    alarm_severity_name: Optional[str] = None
    lower_ctrl_limit: Optional[float] = None
    upper_ctrl_limit: Optional[float] = None
    lower_disp_limit: Optional[float] = None
    upper_disp_limit: Optional[float] = None
    units: Optional[str] = None
    precision: Optional[int] = None

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

    lower_ctrl_limit: Optional[float] = None
    upper_ctrl_limit: Optional[float] = None
    lower_disp_limit: Optional[float] = None
    upper_disp_limit: Optional[float] = None

    units: Optional[str] = None
    precision: Optional[int] = None
    enum_strs: Optional[List[str]] = None

    alarm_status: Optional[str] = None
    alarm_severity: Optional[AlarmSeverity] = None


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
    lower_limit: Optional[float] = None
    upper_limit: Optional[float] = None
    has_limits: bool = False


# ===== Monitoring Subscription Models =====


class PVMonitorRequest(BaseModel):
    """Request to monitor one or more PVs."""

    model_config = ConfigDict(extra="forbid")

    pv_names: List[str]
    update_rate: Optional[float] = Field(None, ge=0.0)
    buffer_size: Optional[int] = Field(None, ge=1, le=1000)


class PVSubscription(BaseModel):
    """Information about an active PV subscription."""

    model_config = ConfigDict(extra="forbid")

    subscription_id: str
    pv_names: List[str]
    status: SubscriptionStatus
    created_at: datetime
    last_update: Optional[datetime] = None
    update_count: int = 0
    client_id: Optional[str] = None


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
    pv: Optional[str] = None
    pv_names: Optional[List[str]] = None
    device: Optional[str] = None
    component: Optional[str] = None
    value: Optional[Any] = None
    timeout: Optional[float] = None


class WebSocketSetRequest(BaseModel):
    """WebSocket set request."""

    model_config = ConfigDict(extra="forbid")

    action: WebSocketAction
    pv: Optional[str] = None
    device: Optional[str] = None
    component: Optional[str] = None
    value: Optional[Any] = None
    timeout: Optional[float] = None


class WebSocketSetResponse(BaseModel):
    """WebSocket set response."""

    model_config = ConfigDict(extra="forbid")

    type: str
    pv: Optional[str] = None
    device: Optional[str] = None
    component: Optional[str] = None
    value: Optional[Any] = None
    success: bool
    message: Optional[str] = None
    timestamp: str


# ===== Nested Component Models =====


class NestedDeviceRequest(BaseModel):
    """Request to access nested device component."""

    model_config = ConfigDict(extra="forbid")

    method: str = "read"
    value: Optional[Any] = None
    timeout: Optional[float] = None


class NestedDeviceResponse(BaseModel):
    """Response from nested device access."""

    model_config = ConfigDict(extra="forbid")

    device_path: str
    method: str
    success: bool
    result: Any = None
    timestamp: datetime
    message: Optional[str] = None


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
    signal: Optional[str] = None
    value: Any
    timestamp: datetime
    connected: bool = True
    read_access: Optional[bool] = True
    write_access: Optional[bool] = None

    _serialize_timestamp = field_serializer("timestamp")(_serialize_unix_epoch)


class DeviceInfo(BaseModel):
    """Device information from configuration service."""

    model_config = ConfigDict(extra="allow")

    name: str
    device_type: str
    ophyd_class: Optional[str] = None
    pvs: Dict[str, str] = Field(default_factory=dict)
    is_movable: bool = False
    is_readable: bool = True


# ===== Stop Operation Models =====


class StopRequest(BaseModel):
    """Request to stop a device/motor."""

    model_config = ConfigDict(extra="forbid")

    timeout: Optional[float] = None


class StopResponse(BaseModel):
    """Response from stop operation."""

    model_config = ConfigDict(extra="forbid")

    pv_name: str
    success: bool
    timestamp: datetime
    message: Optional[str] = None


# ===== Health Response =====


class HealthResponse(BaseModel):
    """Health check response for the merged service."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["healthy", "unhealthy"] = "healthy"
    timestamp: datetime
    coordination_service_available: bool
    coordination_service_detail: Optional[str] = None
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
    detail: Optional[str] = None


# ===== Exceptions =====


class ControlError(Exception):
    """Base exception for control errors."""


class DeviceLockedError(ControlError):
    """Raised when device is locked by active plan."""


class DeviceDisabledError(ControlError):
    """Raised when device is administratively disabled in configuration_service."""


class CoordinationCheckError(ControlError):
    """Raised when coordination check fails."""


class ValueLimitError(ControlError):
    """Raised when value is outside PV limits."""


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
