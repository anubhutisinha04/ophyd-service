"""HTTP client to direct-control's enrichment endpoint.

Used by the path resolver when its static walk returns
``Outcome.NEEDS_ENRICHMENT`` (typically an ophyd ``FormattedComponent``
with a runtime ``{placeholder}``). configuration_service never instantiates
ophyd devices itself; it asks direct-control to do the live introspection
and reports the resolved PV back.

When configuration_service is deployed without a direct-control URL (e.g.
local dev, frontend-only test environments), this client is simply not
constructed and the resolver leaves ``needs_enrichment`` outcomes alone.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import httpx
import structlog

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class EnrichmentSpec:
    """Mirrors the direct-control request shape."""

    device_class_path: str
    prefix: str
    sub_path: str


@dataclass(frozen=True)
class EnrichmentResult:
    """Mirrors one row of the direct-control response."""

    ok: bool
    pv_name: Optional[str] = None
    error_type: Optional[str] = None
    message: Optional[str] = None


class DirectControlUnavailable(Exception):
    """Raised when the direct-control service can't be reached.

    Distinct from "direct-control returned a per-item failure" — that's
    a normal response, returned as ``EnrichmentResult(ok=False, ...)``.
    Service-unreachable means the resolver should mark every batch item
    as ``enrichment_unavailable`` (one transport failure, N affected
    items) rather than fabricating per-item outcomes.
    """


class DirectControlClient:
    """Thin async client around ``POST /api/v1/devices/enrich``.

    Holds its own ``httpx.AsyncClient`` so requests share a connection
    pool. Caller is responsible for ``.aclose()`` at shutdown (the
    lifespan in main.py does this).
    """

    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)
        self._base_url = base_url

    async def enrich(self, specs: List[EnrichmentSpec]) -> List[EnrichmentResult]:
        """Resolve a batch of ``(class, prefix, sub_path)`` triples.

        Returns one ``EnrichmentResult`` per spec, in order. Raises
        ``DirectControlUnavailable`` if direct-control can't be reached
        (network error, timeout, non-2xx response) — the caller can then
        report ``enrichment_unavailable`` for the whole batch instead of
        silently dropping the request.
        """
        payload = {
            "items": [
                {
                    "device_class_path": s.device_class_path,
                    "prefix": s.prefix,
                    "sub_path": s.sub_path,
                }
                for s in specs
            ]
        }

        try:
            resp = await self._client.post("/api/v1/devices/enrich", json=payload)
        except httpx.HTTPError as e:
            logger.warning(
                "direct_control_unreachable",
                base_url=self._base_url,
                error=str(e),
                error_type=type(e).__name__,
            )
            raise DirectControlUnavailable(
                f"direct-control at {self._base_url} unreachable: "
                f"{type(e).__name__}: {e}"
            ) from e

        if resp.status_code != 200:
            logger.warning(
                "direct_control_unexpected_status",
                base_url=self._base_url,
                status_code=resp.status_code,
                body=resp.text[:200],
            )
            raise DirectControlUnavailable(
                f"direct-control returned {resp.status_code}: {resp.text[:200]}"
            )

        # Defend against malformed/garbage responses. A mid-flight wire-
        # contract drift, a reverse proxy stuffing HTML in front of the
        # JSON, or a partial response from a flaky network would all
        # otherwise leave the caller's deferred slots as silent ``None``
        # placeholders that later get filtered out of the response,
        # breaking the 1:1 contract with the input addresses.
        try:
            body = resp.json()
            rows = body["results"]
        except (ValueError, KeyError, TypeError) as e:
            raise DirectControlUnavailable(
                f"direct-control returned malformed body: "
                f"{type(e).__name__}: {e}"
            ) from e

        if not isinstance(rows, list) or len(rows) != len(specs):
            raise DirectControlUnavailable(
                f"direct-control returned {len(rows) if isinstance(rows, list) else type(rows).__name__} "
                f"results for {len(specs)} requests"
            )

        # Row parsing sits under the same malformed-body contract as the
        # envelope above: a row missing "ok" (or not a dict) must degrade to
        # DirectControlUnavailable, not escape as a KeyError → 500.
        try:
            return [
                EnrichmentResult(
                    ok=row["ok"],
                    pv_name=row.get("pv_name"),
                    error_type=row.get("error_type"),
                    message=row.get("message"),
                )
                for row in rows
            ]
        except (KeyError, TypeError, AttributeError) as e:
            raise DirectControlUnavailable(
                f"direct-control returned malformed result row: "
                f"{type(e).__name__}: {e}"
            ) from e

    async def aclose(self) -> None:
        await self._client.aclose()
