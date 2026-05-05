"""
Device controller for executing EPICS commands and Ophyd device methods.

Implements the DeviceControl protocol for commanding devices with
coordination checks (A4 requirement).
"""

import asyncio
from typing import Any, Optional, Dict, TYPE_CHECKING
import structlog
from epics import ca, caget, caput, get_pv
from datetime import datetime

from .models import (
    PVSetRequest,
    PVSetResponse,
    DeviceCommandRequest,
    DeviceCommandResponse,
    CommandMode,
    ControlError,
    DeviceLockedError,
    DeviceDisabledError,
    DeviceLockStatus,
    PVNotFoundError,
)
from .config import Settings

if TYPE_CHECKING:
    from .protocols import CoordinationService
    from .registry_client import RegistryClient


logger = structlog.get_logger(__name__)


class DeviceController:
    """
    Handles device commanding with coordination checks.

    Executes EPICS PV sets and Ophyd device methods, ensuring proper
    coordination with active plan execution (A4 requirement).

    Implements: DeviceControl protocol
    """

    def __init__(
        self,
        settings: Settings,
        coordination: "CoordinationService",
        registry_client: "RegistryClient",
    ):
        """
        Initialize device controller.

        Args:
            settings: Service configuration
            coordination: Coordination service client (implements CoordinationService protocol)
            registry_client: Configuration-service registry client. Used to
                map a PV name to its owning device so the
                disabled/locked-state gate is applied at the device level
                even for PV-keyed writes.
        """
        self.settings = settings
        self.coordination = coordination
        self.registry_client = registry_client

        # Set EPICS environment if configured
        if settings.epics_ca_addr_list:
            import os

            os.environ["EPICS_CA_ADDR_LIST"] = settings.epics_ca_addr_list
            os.environ["EPICS_CA_AUTO_ADDR_LIST"] = (
                "YES" if settings.epics_ca_auto_addr_list else "NO"
            )

    @staticmethod
    def _raise_for_unavailable(target: str, kind: str, coord_status) -> None:
        """If coord_status blocks commands, raise the right typed error.

        DISABLED -> DeviceDisabledError (operator must enable in
            configuration_service before commanding).
        LOCKED -> DeviceLockedError (active plan owns it; release the lock
            or wait for the plan to finish).
        Other non-AVAILABLE statuses -> ControlError (e.g. UNKNOWN — config
            service returned a state we don't model).
        AVAILABLE -> no-op.
        """
        if coord_status.device_available:
            return
        if coord_status.status == DeviceLockStatus.DISABLED:
            logger.warning("device_disabled", **{kind: target})
            raise DeviceDisabledError(
                f"{kind.replace('_', ' ').capitalize()} {target} is disabled in "
                f"configuration_service. Re-enable before commanding."
            )
        if coord_status.status == DeviceLockStatus.LOCKED:
            logger.warning(
                "device_locked", locked_by=coord_status.locked_by, **{kind: target}
            )
            raise DeviceLockedError(
                f"{kind.replace('_', ' ').capitalize()} {target} is locked by "
                f"plan {coord_status.locked_by}"
            )
        logger.warning(
            "device_unavailable",
            status=coord_status.status.value,
            **{kind: target},
        )
        raise ControlError(
            f"{kind.replace('_', ' ').capitalize()} {target} unavailable: "
            f"status={coord_status.status.value}"
        )

    async def set_pv(self, request: PVSetRequest) -> PVSetResponse:
        """
        Set EPICS PV value with coordination check (Low Fidelity Channel).

        Two execution modes based on request.wait:
        - wait=True (put-completion): Waits for EPICS put-completion callback,
          returns confirmed result. Use when confirmation is required.
        - wait=False (fire-and-forget): Issues write immediately without waiting.
          Ideal for motor jogging where user monitors PV readback updates.

        Args:
            request: PV set request

        Returns:
            PV set response with mode indication

        Raises:
            DeviceLockedError: If PV/device is locked by active plan
            DeviceDisabledError: If PV/device is administratively disabled
            ControlError: If set operation fails
        """
        pv_name = request.pv_name
        mode = CommandMode.PUT_COMPLETION if request.wait else CommandMode.FIRE_AND_FORGET

        # Lock/disable state lives at the device level in configuration_service.
        # Map this PV to its owning device so the gate fires correctly. PVs
        # without a device owner (standalone) fall back to the PV name —
        # configuration_service will return 404 for those, and the
        # coordination check treats that as "no lock concept, available".
        coord_target = (
            await self.registry_client.get_owning_device(pv_name)
        ) or pv_name
        coord_status = await self.coordination.check_device_available(coord_target)
        self._raise_for_unavailable(coord_target, "device_name", coord_status)

        # Execute PV set operation. Errors propagate — never return success=False
        # with the requested value as if it had been written; the HTTP layer
        # maps ControlError / Exception to a real 5xx so callers see the failure.
        logger.info(
            "setting_pv",
            pv_name=pv_name,
            value=request.value,
            mode=mode.value,
            wait=request.wait,
        )

        timeout = request.timeout or self.settings.command_timeout
        connection_timeout = request.connection_timeout or 5.0

        success = await self._execute_put(
            pv_name=pv_name,
            value=request.value,
            wait=request.wait,
            timeout=timeout,
            connection_timeout=connection_timeout,
            use_complete=request.use_complete,
            ftype=request.ftype,
        )

        if not success:
            logger.error("pv_set_failed", pv_name=pv_name, value=request.value, mode=mode.value)
            raise ControlError(f"Failed to set PV {pv_name}")

        if mode == CommandMode.FIRE_AND_FORGET:
            logger.info(
                "pv_write_issued",
                pv_name=pv_name,
                value=request.value,
                mode="fire-and-forget",
            )
            return PVSetResponse(
                pv_name=pv_name,
                success=True,
                value_set=request.value,
                timestamp=datetime.now(),
                coordination_checked=True,
                mode=mode,
                message="Write issued (fire-and-forget). Monitor PV readback for confirmation.",
            )

        logger.info(
            "pv_set_confirmed",
            pv_name=pv_name,
            value=request.value,
            mode="put-completion",
        )
        return PVSetResponse(
            pv_name=pv_name,
            success=True,
            value_set=request.value,
            timestamp=datetime.now(),
            coordination_checked=True,
            mode=mode,
            message="PV set confirmed (put-completion)",
        )

    async def execute_device_method(self, request: DeviceCommandRequest) -> DeviceCommandResponse:
        """
        Execute Ophyd device method with coordination check.

        Currently unimplemented: full ophyd-device instantiation requires
        Configuration Service integration that hasn't landed yet. Raises
        ``NotImplementedError`` instead of silently returning ``success=False``
        — the HTTP layer maps that to 501 so safety-relevant calls (e.g. /stop)
        cannot look successful while doing nothing.

        Raises:
            DeviceLockedError: If device is locked by active plan
            DeviceDisabledError: If device is administratively disabled
            NotImplementedError: Always, until ophyd integration lands.
        """
        device_name = request.device_name

        coord_status = await self.coordination.check_device_available(device_name)
        self._raise_for_unavailable(device_name, "device_name", coord_status)

        mode_desc = "put() (no wait)" if request.use_put else "set() (wait for completion)"
        logger.warning(
            "device_method_not_implemented",
            device_name=device_name,
            method=request.method,
            use_put=request.use_put,
            mode=mode_desc,
        )
        raise NotImplementedError(
            f"Device method execution ({request.method} on {device_name}) "
            f"requires Configuration Service ophyd integration; not yet "
            f"implemented. Would use {mode_desc}."
        )

    async def _connect(self, pv_name: str, connection_timeout: float) -> Any:
        """Connect to a PV off-loop; returns the pyepics PV or None on failure."""
        pv = await asyncio.to_thread(get_pv, pv_name, timeout=connection_timeout, connect=True)
        return pv if pv.connected else None

    async def _execute_put(
        self,
        *,
        pv_name: str,
        value: Any,
        wait: bool,
        timeout: float,
        connection_timeout: float,
        use_complete: bool,
        ftype: Optional[int],
    ) -> bool:
        """
        Execute a PV put, routing through the right pyepics entrypoint.

        - No `use_complete` and no `ftype`: use the high-level `caput()`.
        - `use_complete`: use the pyepics put-callback mechanism; the CA thread
          is freed and we await completion via an `asyncio.Event`.
        - `ftype`: drop to `ca.put(chid, ..., ftype=...)` which is the only
          pyepics entrypoint that accepts a forced field type.

        Raises ControlError on connection failure or put-callback timeout so
        the HTTP layer can surface actionable messages.
        """
        if not use_complete and ftype is None:
            status = await asyncio.to_thread(
                caput,
                pv_name,
                value,
                wait=wait,
                timeout=timeout,
                connection_timeout=connection_timeout,
            )
            return bool(status == 1)

        pv = await self._connect(pv_name, connection_timeout)
        if pv is None:
            raise ControlError(f"Failed to connect to PV {pv_name} within {connection_timeout}s")

        if use_complete:
            loop = asyncio.get_running_loop()
            done = asyncio.Event()

            def _cb(**_kw: Any) -> None:
                loop.call_soon_threadsafe(done.set)

            if ftype is not None:
                await asyncio.to_thread(
                    ca.put, pv.chid, value, wait=False, callback=_cb, ftype=ftype
                )
            else:
                await asyncio.to_thread(pv.put, value, use_complete=True, callback=_cb)
            try:
                await asyncio.wait_for(done.wait(), timeout=timeout)
                return True
            except asyncio.TimeoutError:
                raise ControlError(f"PV {pv_name} put-callback did not complete within {timeout}s")

        status = await asyncio.to_thread(
            ca.put, pv.chid, value, wait=wait, timeout=timeout, ftype=ftype
        )
        return bool(status == 1)

    async def get_pv_value(
        self,
        pv_name: str,
        *,
        as_string: bool = False,
        count: Optional[int] = None,
        as_numpy: bool = True,
        use_monitor: bool = True,
        timeout: float = 5.0,
        connection_timeout: float = 5.0,
        ftype: Optional[int] = None,
    ) -> Any:
        """
        Get current PV value (read-only, no coordination check needed).

        Exposes pyepics caget/ca.get knobs so clients can trade off freshness,
        representation, and transport. `ftype=None` uses the native DBR type;
        setting `ftype` forces a non-native type on the wire (rare).

        Raises PVNotFoundError if the PV can't be reached (connection failure,
        caget timeout returning None). Callers should map to an HTTP status —
        no silent None-return fallback.
        """
        if ftype is None:
            value = await asyncio.to_thread(
                caget,
                pv_name,
                as_string=as_string,
                count=count,
                as_numpy=as_numpy,
                use_monitor=use_monitor,
                timeout=timeout,
                connection_timeout=connection_timeout,
            )
            if value is None:
                raise PVNotFoundError(
                    f"PV {pv_name}: caget returned no value (connection or timeout)"
                )
            return value

        # Combine connect + ca.get into one executor hop.
        def _ftype_get() -> Any:
            pv = get_pv(pv_name, timeout=connection_timeout, connect=True)
            if not pv.connected:
                raise PVNotFoundError(
                    f"PV {pv_name}: not connected (timeout {connection_timeout}s)"
                )
            return ca.get(
                pv.chid,
                ftype=ftype,
                count=count,
                timeout=timeout,
                as_string=as_string,
                as_numpy=as_numpy,
            )

        return await asyncio.to_thread(_ftype_get)

    async def access_nested_device(
        self,
        device_path: str,
        method: str = "read",
        value: Optional[Any] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """
        Access nested device component (ophyd-websocket compatible).

        Currently unimplemented for every method: full ophyd-device
        instantiation requires Configuration Service integration that hasn't
        landed yet. Raises ``NotImplementedError`` instead of returning a
        placeholder dict that earlier looked like a successful read/write —
        the HTTP layer maps that to 501. Coord-gate still fires for write
        methods so disabled/locked targets get the precise 409/423 error
        rather than a generic 501.

        Raises:
            DeviceLockedError: If device is locked by active plan (writes only)
            DeviceDisabledError: If device is disabled (writes only)
            NotImplementedError: Always, after the coord gate, until ophyd
                integration lands.
        """
        parts = device_path.split(".")
        device_name = parts[0]
        component_path = parts[1:] if len(parts) > 1 else []

        logger.info(
            "accessing_nested_device",
            device_path=device_path,
            device_name=device_name,
            component_path=component_path,
            method=method,
        )

        # For write operations, perform coordination check.
        # Read methods don't go through the lock/disabled gate — disabled
        # devices can still be monitored, only commanding is blocked.
        if method in ("set", "put", "write", "trigger", "stop"):
            coord_status = await self.coordination.check_device_available(device_name)
            self._raise_for_unavailable(device_name, "device_name", coord_status)

        logger.warning(
            "nested_device_not_implemented",
            device_path=device_path,
            method=method,
        )
        raise NotImplementedError(
            f"Nested device access ({method} on {device_path}) requires "
            f"Configuration Service ophyd integration; not yet implemented."
        )
