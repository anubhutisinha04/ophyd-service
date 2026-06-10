"""Fire-and-forget PV-health reports to configuration_service.

After every caput, direct-control posts the outcome — success or failure
— to ``POST /api/v1/pvs/{pv_name}/{failure|success}`` on
configuration_service. The reports drive the per-PV health state the
operator UI reads via ``GET /api/v1/devices/{name}/status``.

Design intent:

- **Fire-and-forget.** Reporting happens via ``asyncio.create_task`` after
  the caput response has been computed; the caller never awaits it. A
  slow or unreachable configuration_service therefore cannot degrade
  write latency.
- **All errors swallowed.** If config-service is down or rejects the
  report, we log a warning and move on. The caput itself already
  succeeded or failed from the operator's perspective — we don't surface
  a separate "couldn't tell config-service" error.
- **Failure category is narrow.** Only outcomes that actually reflect PV
  health get reported. ``DeviceLockedError`` /
  ``DeviceDisabledError`` / ``CoordinationCheckError`` are *gate*
  failures, not PV failures; we skip those.
- **Tasks are tracked.** ``asyncio.create_task`` returns a task that the
  event loop only weak-references (see Python docs warning); a naive
  fire-and-forget can therefore be GC'd mid-execution. The reporter
  holds strong refs in an in-flight set and removes each via a done
  callback, plus drains them on lifespan shutdown.
"""
from __future__ import annotations

import asyncio
from typing import Optional

import httpx
import structlog

logger = structlog.get_logger(__name__)


async def _post_outcome(
    client: httpx.AsyncClient,
    pv_name: str,
    success: bool,
    message: Optional[str],
) -> None:
    """One POST to config-service's health endpoint. Logs + swallows
    every exception so the in-flight task never raises into the loop."""
    endpoint = "success" if success else "failure"
    body: dict = {"message": message} if message is not None else {}
    try:
        resp = await client.post(
            f"/api/v1/pvs/{pv_name}/{endpoint}",
            json=body,
            timeout=2.0,
        )
        if resp.status_code != 200:
            logger.warning(
                "pv_health_report_non_200",
                pv_name=pv_name,
                endpoint=endpoint,
                status_code=resp.status_code,
                body=resp.text[:200],
            )
    except httpx.HTTPError as e:
        logger.warning(
            "pv_health_report_transport_error",
            pv_name=pv_name,
            endpoint=endpoint,
            error_type=type(e).__name__,
            error=str(e),
        )
    except Exception as e:  # noqa: BLE001 — never propagate to caller
        logger.error(
            "pv_health_report_unexpected",
            pv_name=pv_name,
            endpoint=endpoint,
            error_type=type(e).__name__,
            error=str(e),
            exc_info=True,
        )


class PVHealthReporter:
    """Tracked fire-and-forget reporter.

    Constructed once per service lifetime with the config-service httpx
    client. Each ``report()`` schedules a background POST and retains a
    strong reference until the task completes — so the event loop's
    weak-ref-only behavior can't GC the task mid-execution. ``drain()``
    is called from the lifespan shutdown to flush anything still pending.
    """

    def __init__(self, client: Optional[httpx.AsyncClient]) -> None:
        # ``client=None`` means there is no configuration_service to report
        # to (standalone / file-registry mode). Reporting becomes a no-op —
        # an explicit deployment shape, not a swallowed failure; the
        # lifespan logs it once at startup.
        self._client = client
        self._inflight: set[asyncio.Task] = set()

    def report(
        self, pv_name: str, success: bool, message: Optional[str] = None
    ) -> Optional[asyncio.Task]:
        """Schedule a background POST and return the task.

        Production callers ignore the return value; tests use it to
        ``await`` deterministically. Returns ``None`` in standalone mode
        (no configuration_service configured).
        """
        if self._client is None:
            return None
        task = asyncio.create_task(
            _post_outcome(self._client, pv_name, success, message)
        )
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)
        return task

    async def drain(self, timeout: float = 5.0) -> None:
        """Wait for any in-flight reports to complete during shutdown.

        Cancels anything still pending after ``timeout`` so a hung
        config-service can't block service shutdown indefinitely. After
        cancelling we await the gather once more so the cancellations
        actually propagate before the caller closes shared resources
        (notably the httpx client the tasks depend on).
        """
        if not self._inflight:
            return
        pending = list(self._inflight)
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            still_pending = list(self._inflight)
            logger.warning(
                "pv_health_reporter_drain_timeout",
                pending=len(still_pending),
            )
            for task in still_pending:
                task.cancel()
            # Await again so CancelledError reaches the tasks before
            # the caller closes resources they're still using.
            await asyncio.gather(*still_pending, return_exceptions=True)

    def inflight_count(self) -> int:
        return len(self._inflight)
