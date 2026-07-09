"""
Device controller for executing EPICS commands and Ophyd device methods.

Implements the DeviceControl protocol for commanding devices with
coordination checks (A4 requirement).
"""

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog
from epics import ca, caget, caput, get_pv

from .config import Settings
from .drivers import check_method_allowed, json_safe
from .models import (
    CommandMode,
    ControlError,
    CoordinationCheckError,
    CoordinationStatus,
    DeviceCommandRequest,
    DeviceCommandResponse,
    DeviceDisabledError,
    DeviceLockedError,
    DeviceLockStatus,
    DeviceNotInstantiableError,
    DeviceUnavailableError,
    InstantiationSpec,
    PVNotFoundError,
    PVSetRequest,
    PVSetResponse,
)

if TYPE_CHECKING:
    from .device_manager import DeviceManager
    from .protocols import CoordinationService, RegistryProvider


logger = structlog.get_logger(__name__)


class DeviceController:
    """
    Handles device commanding with coordination checks.

    Executes EPICS PV sets and Ophyd device methods, ensuring proper
    coordination with active plan execution (A4 requirement).

    Implements: DeviceControl protocol

    Coordination is a check-then-act sequence: ``check_device_available``
    reads the device's live lock state from configuration_service, then the
    write is issued. This is inherently TOCTOU — a plan could acquire the
    lock in the window between the check and the caput landing on the IOC.
    The design accepts that narrow race rather than adding a distributed
    reservation, because the exposure is bounded on both ends:

    - configuration_service is authoritative at check time (no local lock
      cache — every check is a live GET /status), so the window is one
      request round-trip, not a TTL;
    - when lock leases are enabled (CONFIG_LOCK_LEASE_TTL_SECONDS), the plan
      owner (queueserver) re-acquires on lease loss / authority reset, and a
      crashed owner's lock lapses on its own — so the coordination state a
      write races against is never stale for longer than the lease.

    A robust closure of the race would require a short-lived write
    reservation on configuration_service; tracked as future work.
    """

    def __init__(
        self,
        settings: Settings,
        coordination: "CoordinationService",
        registry_client: "RegistryProvider",
        device_manager: "DeviceManager",
    ):
        """
        Initialize device controller.

        Args:
            settings: Service configuration
            coordination: Coordination service client (implements CoordinationService protocol)
            registry_client: Registry provider (configuration_service HTTP
                client or file registry). Maps a PV name to its owning device
                so the disabled/locked-state gate is applied at the device
                level even for PV-keyed writes, and supplies the
                instantiation spec for device-level control.
            device_manager: Live-device cache that instantiates + connects
                ophyd / ophyd-async devices from instantiation specs.
        """
        self.settings = settings
        self.coordination = coordination
        self.registry_client = registry_client
        self.device_manager = device_manager

        # Set EPICS environment if configured
        if settings.epics_ca_addr_list:
            import os

            os.environ["EPICS_CA_ADDR_LIST"] = settings.epics_ca_addr_list
            os.environ["EPICS_CA_AUTO_ADDR_LIST"] = (
                "YES" if settings.epics_ca_auto_addr_list else "NO"
            )

    @staticmethod
    def _raise_for_unavailable(target: str, kind: str, coord_status: "CoordinationStatus") -> None:
        """If coord_status blocks commands, raise the right typed error.

        DISABLED -> DeviceDisabledError (operator must enable in
            configuration_service before commanding).
        LOCKED -> DeviceLockedError (active plan owns it; release the lock
            or wait for the plan to finish).
        Other non-AVAILABLE statuses -> DeviceUnavailableError (e.g. UNKNOWN —
            config service returned a state we don't model). This is a
            coordination-policy refusal, NOT a PV-health/EPICS failure, so
            callers must map it like the other gate errors (not to a 500 with
            a PV-health report).
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
            logger.warning("device_locked", locked_by=coord_status.locked_by, **{kind: target})
            raise DeviceLockedError(
                f"{kind.replace('_', ' ').capitalize()} {target} is locked by "
                f"plan {coord_status.locked_by}"
            )
        logger.warning(
            "device_unavailable",
            status=coord_status.status.value,
            **{kind: target},
        )
        raise DeviceUnavailableError(
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
        # A registry FAILURE during this lookup is part of the coordination
        # gate: fail closed (503), never fall through as "standalone" — that
        # would bypass the device-lock gate exactly when the lock authority
        # is unhealthy.
        try:
            owner = await self.registry_client.get_owning_device(pv_name)
        except RuntimeError as e:
            raise CoordinationCheckError(
                f"Cannot determine owning device for PV {pv_name!r}: {e}"
            ) from e
        coord_target = owner or pv_name
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

    async def _require_spec(self, device_name: str) -> "InstantiationSpec":
        """Fetch the device's instantiation spec, failing with a clear,
        actionable error when the registry has no class info for it."""
        spec = await self.registry_client.get_instantiation_spec(device_name)
        if spec is None:
            raise DeviceNotInstantiableError(
                f"Device {device_name!r} has no instantiation spec in the "
                f"registry (class path + constructor args). Device-level "
                f"control requires one; PV-level operations remain available."
            )
        return spec

    async def execute_device_method(self, request: DeviceCommandRequest) -> DeviceCommandResponse:
        """
        Execute an ophyd / ophyd-async device method with coordination check.

        The device is instantiated from its registry instantiation spec and
        cached live (DeviceManager); the framework-matched driver runs the
        method and waits out any returned Status (``use_put=True`` returns
        right after initiation instead).

        Raises:
            MethodNotAllowedError: Method outside the allowlist (HTTP 400)
            DeviceLockedError: Device locked by an active plan (423)
            DeviceDisabledError: Device administratively disabled (409)
            DeviceNotInstantiableError: No instantiation spec in the registry (422)
            ControlError: Instantiation/connect/invoke failure (500)
        """
        device_name = request.device_name
        check_method_allowed(request.method)

        coord_status = await self.coordination.check_device_available(device_name)
        self._raise_for_unavailable(device_name, "device_name", coord_status)

        spec = await self._require_spec(device_name)
        device, driver = await self.device_manager.get_or_connect(spec)

        timeout = request.timeout or self.settings.command_timeout
        logger.info(
            "executing_device_method",
            device_name=device_name,
            method=request.method,
            framework=driver.framework,
            use_put=request.use_put,
            timeout=timeout,
        )
        result = await driver.invoke(
            device,
            request.method,
            request.args,
            request.kwargs,
            timeout=timeout,
            use_put=request.use_put,
            stop_on_timeout=self.settings.stop_on_command_timeout,
        )
        return DeviceCommandResponse(
            device_name=device_name,
            method=request.method,
            success=True,
            result=json_safe(result),
            timestamp=datetime.now(),
            coordination_checked=True,
            message=(
                f"{request.method} initiated (not awaited) via {driver.framework}"
                if request.use_put
                else f"{request.method} completed via {driver.framework}"
            ),
            use_put=request.use_put,
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
        ftype: int | None,
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
            except TimeoutError:
                raise ControlError(
                    f"PV {pv_name} put-callback did not complete within {timeout}s"
                ) from None

        status = await asyncio.to_thread(
            ca.put, pv.chid, value, wait=wait, timeout=timeout, ftype=ftype
        )
        return bool(status == 1)

    async def get_pv_value(
        self,
        pv_name: str,
        *,
        as_string: bool = False,
        count: int | None = None,
        as_numpy: bool = True,
        use_monitor: bool = True,
        timeout: float = 5.0,
        connection_timeout: float = 5.0,
        ftype: int | None = None,
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
        value: Any | None = None,
        timeout: float | None = None,
    ) -> Any:
        """
        Access a nested device component (ophyd-websocket compatible).

        Instantiates the root device from its registry spec, walks the dotted
        component path on the live object, and invokes the method via the
        framework-matched driver. Read methods don't go through the
        lock/disabled gate — disabled devices can still be inspected, only
        commanding is blocked.

        Raises:
            MethodNotAllowedError: Method outside the allowlist, or no value
                supplied for set/put (HTTP 400)
            ComponentNotFoundError: Dotted path doesn't exist on the device (404)
            DeviceLockedError: Device locked by active plan (writes only, 423)
            DeviceDisabledError: Device disabled (writes only, 409)
            DeviceNotInstantiableError: No instantiation spec in the registry (422)
            ControlError: Instantiation/connect/invoke failure (500)
        """
        parts = device_path.split(".")
        device_name = parts[0]
        sub_path = ".".join(parts[1:])

        check_method_allowed(method)

        if method in ("set", "put", "trigger", "stop"):
            coord_status = await self.coordination.check_device_available(device_name)
            self._raise_for_unavailable(device_name, "device_name", coord_status)

        spec = await self._require_spec(device_name)
        device, driver = await self.device_manager.get_or_connect(spec)
        target = await driver.get_component(device, sub_path) if sub_path else device

        if method in ("set", "put"):
            if value is None:
                raise ControlError(f"Method {method!r} on {device_path} requires a value")
            args = [value]
        else:
            args = []

        logger.info(
            "accessing_nested_device",
            device_path=device_path,
            method=method,
            framework=driver.framework,
        )
        result = await driver.invoke(
            target,
            method,
            args,
            {},
            timeout=timeout or self.settings.command_timeout,
            use_put=(method == "put"),
            stop_on_timeout=self.settings.stop_on_command_timeout,
        )
        return json_safe(result)
