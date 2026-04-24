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
)
from .config import Settings

if TYPE_CHECKING:
    from .protocols import CoordinationService


logger = structlog.get_logger(__name__)


class DeviceController:
    """
    Handles device commanding with coordination checks.

    Executes EPICS PV sets and Ophyd device methods, ensuring proper
    coordination with active plan execution (A4 requirement).

    Implements: DeviceControl protocol
    """

    def __init__(self, settings: Settings, coordination: "CoordinationService"):
        """
        Initialize device controller.

        Args:
            settings: Service configuration
            coordination: Coordination service client (implements CoordinationService protocol)
        """
        self.settings = settings
        self.coordination = coordination

        # Set EPICS environment if configured
        if settings.epics_ca_addr_list:
            import os
            os.environ['EPICS_CA_ADDR_LIST'] = settings.epics_ca_addr_list
            os.environ['EPICS_CA_AUTO_ADDR_LIST'] = (
                'YES' if settings.epics_ca_auto_addr_list else 'NO'
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
            ControlError: If set operation fails
        """
        pv_name = request.pv_name
        mode = CommandMode.PUT_COMPLETION if request.wait else CommandMode.FIRE_AND_FORGET

        # Perform coordination check
        # Note: For PV-level control, we use the PV name as device name
        # A more sophisticated implementation might map PVs to devices
        coord_status = await self.coordination.check_device_available(pv_name)

        if not coord_status.device_available:
            logger.warning(
                "device_locked",
                pv_name=pv_name,
                locked_by=coord_status.locked_by,
            )
            raise DeviceLockedError(
                f"PV {pv_name} is locked by plan {coord_status.locked_by}"
            )

        # Execute PV set operation
        try:
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

            if success:
                if mode == CommandMode.FIRE_AND_FORGET:
                    # Fire-and-forget: write issued, client should monitor PV updates
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
                else:
                    # Put-completion: write confirmed
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
            else:
                logger.error("pv_set_failed", pv_name=pv_name, value=request.value, mode=mode.value)
                raise ControlError(f"Failed to set PV {pv_name}")

        except Exception as e:
            logger.error(
                "pv_set_error",
                pv_name=pv_name,
                error=str(e),
                mode=mode.value,
                exc_info=True,
            )
            return PVSetResponse(
                pv_name=pv_name,
                success=False,
                value_set=request.value,
                timestamp=datetime.now(),
                coordination_checked=True,
                mode=mode,
                message=f"Error: {str(e)}",
            )
    
    async def execute_device_method(
        self,
        request: DeviceCommandRequest
    ) -> DeviceCommandResponse:
        """
        Execute Ophyd device method with coordination check.
        
        Args:
            request: Device command request
        
        Returns:
            Device command response
        
        Raises:
            DeviceLockedError: If device is locked by active plan
            ControlError: If command execution fails
        """
        device_name = request.device_name
        
        # Perform coordination check
        coord_status = await self.coordination.check_device_available(device_name)
        
        if not coord_status.device_available:
            logger.warning(
                "device_locked",
                device_name=device_name,
                locked_by=coord_status.locked_by,
            )
            raise DeviceLockedError(
                f"Device {device_name} is locked by plan {coord_status.locked_by}"
            )
        
        # Execute device method
        # Note: Full Ophyd device loading would require Configuration Service integration
        # This is a simplified implementation for the pip-installable pattern
        try:
            logger.info(
                "executing_device_method",
                device_name=device_name,
                method=request.method,
                args=request.args,
                kwargs=request.kwargs,
                use_put=request.use_put,
            )

            # In a full implementation, we would:
            # 1. Query Configuration Service for device definition
            # 2. Instantiate Ophyd device
            # 3. Execute method using put() or set() based on use_put flag:
            #    - use_put=True: device.put(value) - returns immediately
            #    - use_put=False: device.set(value) - waits for Status.done
            # For now, return a placeholder

            mode_desc = "put() (no wait)" if request.use_put else "set() (wait for completion)"
            logger.warning(
                "device_method_not_implemented",
                device_name=device_name,
                method=request.method,
                use_put=request.use_put,
                note=f"Full Ophyd device execution requires Configuration Service integration. Would use {mode_desc}",
            )

            return DeviceCommandResponse(
                device_name=device_name,
                method=request.method,
                success=False,
                result=None,
                timestamp=datetime.now(),
                coordination_checked=True,
                message=f"Device method execution requires Configuration Service integration. Mode: {mode_desc}",
                use_put=request.use_put,
            )

        except Exception as e:
            logger.error(
                "device_method_error",
                device_name=device_name,
                method=request.method,
                error=str(e),
                exc_info=True,
            )
            return DeviceCommandResponse(
                device_name=device_name,
                method=request.method,
                success=False,
                result=None,
                timestamp=datetime.now(),
                coordination_checked=True,
                message=f"Error: {str(e)}",
                use_put=request.use_put,
            )
    
    async def _connect(self, pv_name: str, connection_timeout: float):
        """Connect to a PV off-loop; returns the pyepics PV or None on failure."""
        pv = await asyncio.to_thread(
            get_pv, pv_name, timeout=connection_timeout, connect=True
        )
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
            return bool(status and status > 0)

        pv = await self._connect(pv_name, connection_timeout)
        if pv is None:
            raise ControlError(
                f"Failed to connect to PV {pv_name} within {connection_timeout}s"
            )

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
                raise ControlError(
                    f"PV {pv_name} put-callback did not complete within {timeout}s"
                )

        status = await asyncio.to_thread(
            ca.put, pv.chid, value, wait=wait, timeout=timeout, ftype=ftype
        )
        return status == 1

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
    ) -> Optional[Any]:
        """
        Get current PV value (read-only, no coordination check needed).

        Exposes pyepics caget/ca.get knobs so clients can trade off freshness,
        representation, and transport. `ftype=None` uses the native DBR type;
        setting `ftype` forces a non-native type on the wire (rare).
        """
        try:
            if ftype is None:
                return await asyncio.to_thread(
                    caget,
                    pv_name,
                    as_string=as_string,
                    count=count,
                    as_numpy=as_numpy,
                    use_monitor=use_monitor,
                    timeout=timeout,
                    connection_timeout=connection_timeout,
                )

            # Combine connect + ca.get into one executor hop.
            def _ftype_get() -> Optional[Any]:
                pv = get_pv(pv_name, timeout=connection_timeout, connect=True)
                if not pv.connected:
                    return None
                return ca.get(
                    pv.chid,
                    ftype=ftype,
                    count=count,
                    timeout=timeout,
                    as_string=as_string,
                    as_numpy=as_numpy,
                )

            return await asyncio.to_thread(_ftype_get)
        except Exception as e:
            logger.error("get_pv_error", pv_name=pv_name, error=str(e))
            return None

    async def access_nested_device(
        self,
        device_path: str,
        method: str = "read",
        value: Optional[Any] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """
        Access nested device component (ophyd-websocket compatible).

        Supports dot-separated paths like:
        - motor1
        - motor1.user_readback
        - detector.image.array_size

        Args:
            device_path: Dot-separated device component path
            method: Method to execute (read, set, trigger, etc.)
            value: Value to set (for set method)
            timeout: Timeout in seconds

        Returns:
            Result of the operation

        Raises:
            DeviceLockedError: If device is locked by active plan
            ControlError: If operation fails
        """
        # Parse device path
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

        # For write operations, perform coordination check
        if method in ("set", "put", "write", "trigger", "stop"):
            coord_status = await self.coordination.check_device_available(device_name)

            if not coord_status.device_available:
                logger.warning(
                    "device_locked",
                    device_path=device_path,
                    locked_by=coord_status.locked_by,
                )
                raise DeviceLockedError(
                    f"Device {device_name} is locked by plan {coord_status.locked_by}"
                )

        # In a full implementation, we would:
        # 1. Query Configuration Service for device definition
        # 2. Instantiate Ophyd device
        # 3. Navigate to the nested component
        # 4. Execute method
        #
        # For now, we implement a simplified version that works with EPICS PVs
        # where the component path maps to PV suffixes
        #
        # Example: motor1.user_readback -> IOC:motor1.RBV or IOC:motor1:RBV

        # Placeholder implementation - returns mock data or attempts PV access
        try:
            if method in ("read", "get"):
                # Try to interpret as PV pattern
                # This is a simplified mapping - real implementation would query config service
                logger.warning(
                    "nested_device_read_placeholder",
                    device_path=device_path,
                    note="Full Ophyd device access requires Configuration Service integration",
                )
                return {
                    "device_path": device_path,
                    "method": method,
                    "status": "placeholder",
                    "note": "Full Ophyd device access requires Configuration Service integration",
                }

            elif method in ("set", "put", "write"):
                logger.warning(
                    "nested_device_set_placeholder",
                    device_path=device_path,
                    value=value,
                    note="Full Ophyd device access requires Configuration Service integration",
                )
                return {
                    "device_path": device_path,
                    "method": method,
                    "value": value,
                    "status": "placeholder",
                    "note": "Full Ophyd device access requires Configuration Service integration",
                }

            else:
                logger.warning(
                    "nested_device_method_placeholder",
                    device_path=device_path,
                    method=method,
                    note="Full Ophyd device access requires Configuration Service integration",
                )
                return {
                    "device_path": device_path,
                    "method": method,
                    "status": "placeholder",
                    "note": "Full Ophyd device access requires Configuration Service integration",
                }

        except Exception as e:
            logger.error(
                "nested_device_error",
                device_path=device_path,
                method=method,
                error=str(e),
                exc_info=True,
            )
            raise ControlError(f"Failed to access {device_path}: {str(e)}")
