"""
HTTP client for the bluesky-configuration-service.

This module is imported only when the ``config_service`` feature is enabled
in the server configuration. Nothing at module scope pulls in ``httpx`` or
touches the network; the legacy code path stays clean when the feature is
off.

Policy (see feedback_backwards_compat memory):
- Enabled vs. disabled is a binary operator choice. There is NO "enabled but
  silently degraded" runtime state.
- Transient network/upstream errors (ConnectError, ReadTimeout, 502/503/504)
  are retried up to ``max_attempts`` times with small linear backoff; each
  retry is logged at WARNING. On exhaustion, the last exception is raised.
- All other errors (4xx, 5xx other than 502/503/504, malformed JSON) raise
  immediately without retry.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Any, Collection, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_MS = (200, 400)
DEFAULT_SERVICE_NAME = "bluesky-queueserver"

_RETRYABLE_HTTP_STATUS = frozenset({502, 503, 504})

_VALID_LOCK_SCOPES = frozenset({"environment", "plan"})

# Sentinel placed in a manager command's failure envelope under ``error_kind``
# when the failure was config-service being unreachable (network error or 5xx
# exhaustion). The HTTP layer maps this to 503 Service Unavailable instead of a
# generic 409, so an upstream outage is not misreported as a conflict.
ERROR_KIND_CONFIG_SERVICE_UNREACHABLE = "config_service_unreachable"


class ConfigServiceError(Exception):
    """Base class for configuration-service client errors."""


class ConfigServiceUnreachable(ConfigServiceError):
    """Network-level failures that persisted through all retry attempts."""


class ConfigServiceHTTPError(ConfigServiceError):
    """HTTP error response (status code, body)."""

    def __init__(self, status_code: int, body: Any, message: str = ""):
        self.status_code = status_code
        self.body = body
        super().__init__(message or f"HTTP {status_code}: {body!r}")


class ConfigServiceConflict(ConfigServiceHTTPError):
    """409 — resource already exists, lock already held by another owner, etc."""


class ConfigServiceNotFound(ConfigServiceHTTPError):
    """404 — resource does not exist."""


class ConfigServiceProtocolError(ConfigServiceError):
    """Unexpected response shape (missing fields, wrong types)."""


@dataclasses.dataclass(frozen=True)
class ConfigServiceState:
    """State captured at env-open and advanced by the pre-plan staleness check.

    ``cursor`` is the audit-log id the client has already applied; the next
    /devices/changes call uses it as ``since_version``. ``epoch`` is the
    service-instance identifier; a mismatch on a later call means the cursor
    is invalid and the client must re-fetch the full registry.
    """

    cursor: int = 0
    epoch: str = ""


@dataclasses.dataclass(frozen=True)
class ConfigServiceSettings:
    """Parsed ``config_service`` section of the server configuration."""

    enabled: bool = False
    url: str = ""
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    backoff_ms: Tuple[int, ...] = DEFAULT_BACKOFF_MS
    service_name: str = DEFAULT_SERVICE_NAME
    # Lock scope — which devices queueserver locks in configuration_service and
    # for how long. Default is "plan".
    #
    # "plan" (default): no environment lock; before each plan starts, lock
    #   exactly the registered devices referenced by the plan's args/kwargs,
    #   release when the plan reaches a terminal state. Devices are therefore
    #   free for direct control whenever no plan is running (idle env → free).
    #   Combine with configuration_service's lock_all policy to choose between
    #   the two operator-facing variants:
    #     - lock_all=False → only the plan's devices are locked (variant 2);
    #     - lock_all=True  → while a plan runs, EVERY registered device reports
    #       locked, even ones the plan doesn't use (variant 1). Availability is
    #       widened on the config-service side; queueserver still only acquires
    #       the plan's devices, so idle → free still holds.
    # "environment": one lock over every registry device for the lifetime of
    #   the worker environment (devices stay locked even when idle). Kept for
    #   deployments that want the beamline fully owned by queueserver whenever
    #   an environment is open.
    lock_scope: str = "plan"

    @classmethod
    def from_config_dict(cls, section: Optional[Dict[str, Any]]) -> "ConfigServiceSettings":
        if not section:
            return cls()
        enabled = bool(section.get("enabled", False))
        if not enabled:
            return cls(enabled=False)

        if "url" not in section or not section["url"]:
            raise ValueError(
                "config_service.enabled is true but config_service.url is not set. "
                "Set url explicitly (there is no default)."
            )
        url = section["url"]
        timeout = float(section.get("timeout", DEFAULT_TIMEOUT_SECONDS))

        max_attempts = int(section.get("max_attempts", DEFAULT_MAX_ATTEMPTS))
        if max_attempts < 1:
            raise ValueError(
                f"config_service.max_attempts must be >= 1 (got {max_attempts!r})"
            )

        raw_backoff = section.get("backoff_ms", list(DEFAULT_BACKOFF_MS))
        if not isinstance(raw_backoff, (list, tuple)):
            raise ValueError(
                "config_service.backoff_ms must be a list of integers "
                f"(got {raw_backoff!r})"
            )
        backoff_ms = tuple(int(x) for x in raw_backoff)

        service_name = str(section.get("service_name", DEFAULT_SERVICE_NAME))

        lock_scope = str(section.get("lock_scope", "plan"))
        if lock_scope not in _VALID_LOCK_SCOPES:
            raise ValueError(
                f"config_service.lock_scope must be one of "
                f"{sorted(_VALID_LOCK_SCOPES)} (got {lock_scope!r})"
            )

        return cls(
            enabled=True,
            url=url,
            timeout=timeout,
            max_attempts=max_attempts,
            backoff_ms=backoff_ms,
            service_name=service_name,
            lock_scope=lock_scope,
        )


class ConfigServiceClient:
    """Async REST client for bluesky-configuration-service.

    Construct only when ``settings.enabled`` is True. The constructor
    lazy-imports ``httpx``; callers on the legacy path should never reach
    this class, so ``httpx`` remains out of the legacy dependency graph.
    """

    def __init__(
        self,
        settings: ConfigServiceSettings,
        *,
        transport: Any = None,
    ) -> None:
        if not settings.enabled:
            raise ValueError(
                "ConfigServiceClient constructed with disabled settings — "
                "this is a caller bug; guard on settings.enabled"
            )
        import httpx

        self._settings = settings
        self._httpx = httpx
        self._client = httpx.AsyncClient(
            base_url=settings.url.rstrip("/"),
            timeout=httpx.Timeout(settings.timeout),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "ConfigServiceClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    # Public API ---------------------------------------------------------

    async def get_devices_info(self) -> Dict[str, Any]:
        """Return registry metadata keyed by device name (no instantiation specs)."""
        return await self._request("GET", "/api/v1/devices-info")

    async def get_instantiation_specs(self) -> Dict[str, Dict[str, Any]]:
        """Return ``{name: DeviceInstantiationSpec}`` for every device in the registry."""
        body = await self._request("GET", "/api/v1/devices/instantiation")
        if not isinstance(body, dict):
            raise ConfigServiceProtocolError(
                f"/devices/instantiation returned non-dict body: {type(body).__name__}"
            )
        return body

    async def is_registry_empty(self) -> bool:
        info = await self.get_devices_info()
        if not isinstance(info, dict):
            raise ConfigServiceProtocolError(
                f"/devices-info returned non-dict body: {type(info).__name__}"
            )
        return len(info) == 0

    async def get_changes_since(self, since_version: int) -> Dict[str, Any]:
        """Return delta payload from GET /api/v1/devices/changes."""
        body = await self._request(
            "GET",
            "/api/v1/devices/changes",
            params={"since_version": since_version},
        )
        for key in ("current_version", "service_epoch", "reset_occurred", "changes"):
            if key not in body:
                raise ConfigServiceProtocolError(
                    f"/devices/changes response missing {key!r}: {body!r}"
                )
        return body

    async def upsert_device(
        self,
        metadata: Dict[str, Any],
        spec: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Create if the device name is new, otherwise update."""
        name = metadata.get("name")
        if not name:
            raise ValueError("device metadata is missing 'name'")
        payload: Dict[str, Any] = {"metadata": metadata}
        if spec is not None:
            payload["instantiation_spec"] = spec
        try:
            return await self._request(
                "POST", "/api/v1/devices", json=payload, expected_status=(200, 201)
            )
        except ConfigServiceConflict:
            return await self._request(
                "PUT", f"/api/v1/devices/{name}", json=payload
            )

    async def delete_device(self, name: str) -> Dict[str, Any]:
        return await self._request("DELETE", f"/api/v1/devices/{name}")

    async def lock_devices(
        self,
        device_names: List[str],
        *,
        item_id: str,
        plan_name: str,
    ) -> Dict[str, Any]:
        payload = {
            "device_names": list(device_names),
            "item_id": item_id,
            "plan_name": plan_name,
            "locked_by_service": self._settings.service_name,
        }
        return await self._request("POST", "/api/v1/devices/lock", json=payload)

    async def force_unlock_devices(
        self,
        device_names: List[str],
        *,
        reason: str,
    ) -> Dict[str, Any]:
        """Administrative override that clears locks regardless of ownership.

        Used by the env-open lock path to recover from orphaned locks left
        by a dead previous manager incarnation. Without this the
        watchdog would restart the manager forever into the same 409.
        Audit-logged on the config-service side; reason should identify the
        recovery context.
        """
        payload = {
            "device_names": list(device_names),
            "reason": reason,
        }
        return await self._request(
            "POST", "/api/v1/devices/force-unlock", json=payload
        )

    async def unlock_devices(
        self,
        device_names: List[str],
        *,
        item_id: str,
    ) -> Dict[str, Any]:
        payload = {
            "device_names": list(device_names),
            "item_id": item_id,
        }
        return await self._request("POST", "/api/v1/devices/unlock", json=payload)

    async def renew_locks(
        self,
        device_names: List[str],
        *,
        item_id: str,
    ) -> Dict[str, Any]:
        """Extend the lease on locks held by ``item_id`` (heartbeat).

        Returns the parsed renew response: ``renewed_devices``, ``lost_devices``,
        ``conflict_devices``, ``lock_epoch`` and ``expires_at``. A ``lost``
        device (or a changed ``lock_epoch``) tells the caller the authority
        dropped the lock and it must be re-acquired.
        """
        payload = {
            "device_names": list(device_names),
            "item_id": item_id,
        }
        return await self._request("POST", "/api/v1/devices/lock/renew", json=payload)

    # Internals ----------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        expected_status: Tuple[int, ...] = (200, 201, 204),
    ) -> Any:
        httpx = self._httpx
        # httpx.TimeoutException is the common base of ConnectTimeout,
        # ReadTimeout, WriteTimeout, and PoolTimeout — using the base
        # ensures TCP-handshake timeouts (ConnectTimeout) are retried
        # alongside the other timeout flavors instead of escaping raw.
        retryable_exc = (
            httpx.ConnectError,
            httpx.TimeoutException,
        )
        attempts = self._settings.max_attempts
        last_exc: Optional[BaseException] = None

        for attempt in range(1, attempts + 1):
            try:
                response = await self._client.request(
                    method, path, params=params, json=json
                )
            except retryable_exc as exc:
                last_exc = exc
                if attempt < attempts:
                    delay_ms = self._backoff_for_attempt(attempt)
                    logger.warning(
                        "config-service %s %s failed (attempt %d/%d): %s — retrying in %dms",
                        method, path, attempt, attempts, exc, delay_ms,
                    )
                    await asyncio.sleep(delay_ms / 1000.0)
                    continue
                logger.error(
                    "config-service %s %s failed after %d attempts: %s",
                    method, path, attempts, exc,
                )
                raise ConfigServiceUnreachable(
                    f"{method} {path} failed after {attempts} attempts: {exc}"
                ) from exc
            except httpx.TransportError as exc:
                # Belt-and-braces: any other transport-layer httpx exception
                # (e.g. ProxyError, UnsupportedProtocol, NetworkError variants
                # not already covered above) is wrapped so a raw httpx type
                # never escapes this boundary into manager's typed handlers.
                # Narrower than httpx.HTTPError on purpose: HTTPStatusError
                # only fires via response.raise_for_status() which this
                # client never calls (status codes are inspected manually
                # below), so it cannot reach here today; and DecodingError /
                # TooManyRedirects are not transport faults and would be
                # better surfaced as ProtocolError-style failures than
                # silently masked as "unreachable".
                logger.error(
                    "config-service %s %s failed with unexpected httpx transport error: %s",
                    method, path, exc,
                )
                raise ConfigServiceUnreachable(
                    f"{method} {path} failed with unexpected httpx transport error: {exc}"
                ) from exc

            if response.status_code in _RETRYABLE_HTTP_STATUS:
                last_exc = ConfigServiceHTTPError(
                    response.status_code, _safe_body(response)
                )
                if attempt < attempts:
                    delay_ms = self._backoff_for_attempt(attempt)
                    logger.warning(
                        "config-service %s %s returned %d (attempt %d/%d) — retrying in %dms",
                        method, path, response.status_code, attempt, attempts, delay_ms,
                    )
                    await asyncio.sleep(delay_ms / 1000.0)
                    continue
                logger.error(
                    "config-service %s %s returned %d after %d attempts",
                    method, path, response.status_code, attempts,
                )
                raise ConfigServiceUnreachable(
                    f"{method} {path} returned {response.status_code} after {attempts} attempts"
                ) from last_exc

            if response.status_code in expected_status:
                if response.status_code == 204 or not response.content:
                    return {}
                try:
                    return response.json()
                except Exception as exc:
                    raise ConfigServiceProtocolError(
                        f"{method} {path} returned non-JSON body: {exc}"
                    ) from exc

            body = _safe_body(response)
            if response.status_code == 404:
                raise ConfigServiceNotFound(response.status_code, body)
            if response.status_code == 409:
                raise ConfigServiceConflict(response.status_code, body)
            raise ConfigServiceHTTPError(response.status_code, body)

        assert last_exc is not None
        raise ConfigServiceUnreachable(str(last_exc)) from last_exc

    def _backoff_for_attempt(self, attempt: int) -> int:
        schedule = self._settings.backoff_ms
        if not schedule:
            return 0
        idx = min(attempt - 1, len(schedule) - 1)
        return schedule[idx]


def _safe_body(response: Any) -> Any:
    try:
        return response.json()
    except Exception:
        try:
            return response.text
        except Exception:
            return None


async def _bootstrap_with_retry(
    client: "ConfigServiceClient",
    device_data: Dict[str, Dict[str, Any]],
) -> None:
    """Upsert every device into an empty registry; retry failures once.

    The first env-open against an empty registry has to insert every
    device the worker knows about. A bare ``asyncio.gather`` over those
    upserts cancels its siblings on the first failure, leaving the
    registry partially populated. On the next env-open the is-empty
    gate sees a non-empty registry, skips bootstrap, and the change
    feed never delivers the missing devices — permanent partial state
    until an operator intervenes.

    We instead run the gather with ``return_exceptions=True``, retry
    exactly the names that failed (once), and raise loudly if anything
    is still missing. The retry is bounded so a genuinely broken
    upstream surfaces immediately rather than looping; the loud raise
    upholds the no-silent-fallback rule. The first underlying error is
    chained onto the raised ``ConfigServiceError`` (``__cause__``) so a
    debugger landing on the raise site still has the original traceback.

    Names are upserted in sorted order in both passes so the per-attempt
    log line is reproducible.

    Only ``Exception`` subclasses are counted as per-device failures.
    ``asyncio.CancelledError``, ``KeyboardInterrupt``, and
    ``SystemExit`` (the non-``Exception`` ``BaseException`` subclasses)
    are propagated immediately so cancellation and process shutdown
    behave normally instead of being aggregated into a bootstrap error.
    """
    entries = sorted(device_data.items())  # [(name, payload), ...]

    async def _attempt(
        targets: List[Tuple[str, Dict[str, Any]]],
    ) -> Dict[str, Exception]:
        results = await asyncio.gather(
            *(
                client.upsert_device(payload["metadata"], payload["spec"])
                for _, payload in targets
            ),
            return_exceptions=True,
        )
        failures: Dict[str, Exception] = {}
        for (name, _), result in zip(targets, results):
            if isinstance(result, Exception):
                failures[name] = result
            elif isinstance(result, BaseException):
                # CancelledError / KeyboardInterrupt / SystemExit — these
                # are not per-device failures; propagate so cancellation
                # and shutdown work as the caller expects.
                raise result
        return failures

    failures = await _attempt(entries)
    if failures:
        logger.warning(
            "config-service bootstrap: %d device(s) failed first attempt; "
            "retrying: %s",
            len(failures),
            sorted(failures),
        )
        retry_entries = [(name, device_data[name]) for name in sorted(failures)]
        failures = await _attempt(retry_entries)

    if failures:
        ordered = sorted(failures.items())
        detail = ", ".join(
            f"{name}: {type(exc).__name__}: {exc}" for name, exc in ordered
        )
        # Chain the first remaining failure as __cause__ so a debugger
        # landing on the raise site has the original traceback even
        # when several devices failed.
        first_cause = ordered[0][1]
        raise ConfigServiceError(
            "config-service bootstrap failed after retry for "
            f"{len(failures)} device(s): {detail}"
        ) from first_cause


async def sync_devices_on_env_open(
    client: ConfigServiceClient,
    expected_device_names: List[str],
    device_data: Dict[str, Dict[str, Any]],
    prefetched_info: Optional[Dict[str, Any]] = None,
) -> ConfigServiceState:
    """Bootstrap-if-empty and capture the version cursor.

    Raises ``ConfigServiceError`` if ``device_data`` is missing entries for
    any expected device (i.e. worker-side introspection couldn't produce
    a payload for a device the manager thinks exists). Per the no-silent-
    fallback rule, we do not proceed with partial registry contents.

    ``prefetched_info`` — when the manager already fetched /devices-info at
    the start of env-open (Layer 2.6 consume-mode), pass the result here to
    skip the redundant probe. ``None`` means "I don't know, ask the service".
    """
    missing = [name for name in expected_device_names if name not in device_data]
    if missing:
        raise ConfigServiceError(
            "config-service sync aborted: device introspection did not produce "
            "metadata/spec for the following devices (check worker logs for "
            f"per-device extraction failures): {sorted(missing)!r}"
        )

    is_empty = (
        len(prefetched_info) == 0
        if prefetched_info is not None
        else await client.is_registry_empty()
    )
    if is_empty:
        logger.info(
            "config-service registry is empty; bootstrapping %d device(s)",
            len(device_data),
        )
        await _bootstrap_with_retry(client, device_data)
        logger.info("config-service bootstrap complete (%d device(s))", len(device_data))
    else:
        logger.info(
            "config-service registry is populated; skipping bootstrap"
        )

    changes = await client.get_changes_since(0)
    return ConfigServiceState(
        cursor=int(changes["current_version"]),
        epoch=str(changes["service_epoch"]),
    )


@dataclasses.dataclass(frozen=True)
class StalenessPlan:
    """Actionable result of a pre-plan staleness check.

    ``replace_overlay`` is True when config-service reported a reset or a
    service_epoch change — the worker's previous overlay is untrusted and
    must be dropped wholesale; ``upserts`` then carries the full registry
    (populated by ``fetch_staleness_plan`` with a follow-up
    /devices/instantiation fetch). Otherwise the plan describes an
    incremental diff: ``upserts`` + ``deletes`` are exactly what changed
    since ``state.cursor``. ``new_state`` is the cursor+epoch to commit
    after the worker accepts the overlay.
    """

    replace_overlay: bool
    upserts: Dict[str, Dict[str, Any]]
    deletes: List[str]
    new_state: ConfigServiceState

    @property
    def is_noop(self) -> bool:
        return not self.replace_overlay and not self.upserts and not self.deletes


def build_staleness_plan(
    response: Dict[str, Any], saved_epoch: str
) -> StalenessPlan:
    """Turn a /devices/changes response into an overlay-update plan.

    Pure function. ``fetch_staleness_plan`` wraps it with the full-refetch
    HTTP call when ``replace_overlay`` is True.
    """
    new_state = ConfigServiceState(
        cursor=int(response["current_version"]),
        epoch=str(response["service_epoch"]),
    )
    if response["reset_occurred"] or response["service_epoch"] != saved_epoch:
        # upserts left empty here; fetch_staleness_plan fills it from
        # /devices/instantiation so the caller sees the full registry.
        return StalenessPlan(
            replace_overlay=True, upserts={}, deletes=[], new_state=new_state
        )

    upserts: Dict[str, Dict[str, Any]] = {}
    deletes: List[str] = []
    for change in response["changes"]:
        name = change["device_name"]
        op = change["op"]
        if op == "upsert":
            spec = change.get("spec")
            if spec is None:
                raise ConfigServiceProtocolError(
                    f"upsert change for {name!r} is missing 'spec'"
                )
            # An upsert with active=false means the device was disabled.
            # The full-refresh path fetches /devices/instantiation with
            # active_only=True (the server-side default), which silently
            # drops disabled devices. Mirror that here so the incremental
            # path agrees: a disable removes the device from the overlay
            # instead of re-instantiating it. ``active`` is optional in
            # the wire format (older clients may omit it); treat absent
            # as True so unrelated changes keep their previous meaning.
            if spec.get("active", True) is False:
                deletes.append(name)
            else:
                upserts[name] = spec
        elif op == "delete":
            deletes.append(name)
        else:
            raise ConfigServiceProtocolError(
                f"unknown change op {op!r} for device {name!r}"
            )

    return StalenessPlan(
        replace_overlay=False,
        upserts=upserts,
        deletes=deletes,
        new_state=new_state,
    )


async def fetch_staleness_plan(
    client: ConfigServiceClient,
    state: ConfigServiceState,
) -> StalenessPlan:
    """Call /devices/changes; on epoch-mismatch or registry reset also fetch
    /devices/instantiation so the caller can apply a single atomic overlay
    replacement without a second round-trip."""
    response = await client.get_changes_since(state.cursor)
    plan = build_staleness_plan(response, state.epoch)
    if plan.replace_overlay:
        specs = await client.get_instantiation_specs()
        plan = dataclasses.replace(plan, upserts=specs)
    return plan


# ---------------------------------------------------------------------------
# Device-registry diff + apply (powers the device-diff endpoints)
# ---------------------------------------------------------------------------


# Strategies accepted by ``apply_diff``. Kept in module scope so the HTTP
# router and tests can reference the same constant.
APPLY_STRATEGIES = ("all", "additions_only", "selected")


@dataclasses.dataclass(frozen=True)
class DeviceDiff:
    """Profile-collection vs config-service registry diff.

    ``added`` — names the running worker introspected that are NOT in the
    registry. ``removed`` — names in the registry that the running worker
    no longer reports. ``modified`` — entries whose normalized instantiation
    spec differs between profile and registry; each item is a dict with
    ``name``, ``before`` (registry spec), ``after`` (worker spec), and
    ``fields_changed`` (sorted top-level keys whose values differ).

    Lists are sorted by name for reproducible logs and stable API output.
    """

    added: List[str]
    removed: List[str]
    modified: List[Dict[str, Any]]

    @property
    def is_empty(self) -> bool:
        return not self.added and not self.removed and not self.modified

    def to_dict(self) -> Dict[str, Any]:
        return {
            "added": list(self.added),
            "removed": list(self.removed),
            "modified": [dict(item) for item in self.modified],
        }


def _spec_from_device_payload(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Pull the instantiation spec out of a worker device-data payload.

    Worker payloads have the shape ``{"metadata": {...}, "spec": {...}}``;
    the spec is what config-service stores under ``instantiation_spec``.
    Returns ``None`` if the payload doesn't carry a spec (e.g. a metadata-
    only entry) — the diff treats spec-less worker entries as "matches
    anything" for the modified-detection pass, since there's nothing to
    compare structurally.
    """
    if not isinstance(payload, dict):
        return None
    spec = payload.get("spec")
    if spec is None:
        return None
    return spec


def _changed_fields(before: Dict[str, Any], after: Dict[str, Any]) -> List[str]:
    """Top-level keys whose values differ between ``before`` and ``after``.

    Compares values with ``==``; nested dicts are compared by structural
    equality (Python default), which matches issue #61's "structural
    equality over the payloads already sent to config_service".
    """
    keys = set(before.keys()) | set(after.keys())
    return sorted(k for k in keys if before.get(k) != after.get(k))


def compute_diff(
    device_data: Dict[str, Dict[str, Any]],
    registry_specs: Dict[str, Dict[str, Any]],
) -> DeviceDiff:
    """Compare the running worker's introspected devices to the registry.

    Pure function. ``device_data`` is the worker payload dict the manager
    already carries on ``self._config_service_device_data`` (shape:
    ``{name: {"metadata": ..., "spec": ...}}``). ``registry_specs`` is
    the result of ``ConfigServiceClient.get_instantiation_specs()``
    (shape: ``{name: spec}``).

    The ``modified`` bucket only includes names present in both sides
    whose specs differ. An entry whose worker payload carries no
    ``spec`` is skipped from the modified pass (there's nothing
    structural to compare); the name still appears in ``added`` if
    missing from the registry.
    """
    profile_names = set(device_data.keys())
    registry_names = set(registry_specs.keys())

    added = sorted(profile_names - registry_names)
    removed = sorted(registry_names - profile_names)

    modified: List[Dict[str, Any]] = []
    for name in sorted(profile_names & registry_names):
        after = _spec_from_device_payload(device_data[name])
        if after is None:
            continue
        before = registry_specs[name]
        if before == after:
            continue
        modified.append(
            {
                "name": name,
                "before": before,
                "after": after,
                "fields_changed": _changed_fields(before, after),
            }
        )

    return DeviceDiff(added=added, removed=removed, modified=modified)


def _validate_strategy(strategy: str) -> None:
    if strategy not in APPLY_STRATEGIES:
        raise ValueError(
            f"unknown strategy {strategy!r}; expected one of {APPLY_STRATEGIES!r}"
        )


def _select_writes(
    diff: DeviceDiff,
    *,
    strategy: str,
    selected: Optional[Collection[str]],
) -> Tuple[List[str], List[str]]:
    """Resolve (upserts, deletes) name lists from a diff + strategy.

    ``"all"`` — every added/modified upserted, every removed deleted.
    ``"additions_only"`` — only the ``added`` list is upserted; removed and
        modified are left alone (cheapest, never destroys data).
    ``"selected"`` — caller-supplied ``selected`` set restricted to the
        names that actually appear in the diff. Selections that don't
        match anything in the diff are silently dropped (they're already
        in sync); a non-collection ``selected`` is rejected.
    """
    _validate_strategy(strategy)
    modified_names = [item["name"] for item in diff.modified]

    if strategy == "all":
        return (sorted(diff.added + modified_names), list(diff.removed))

    if strategy == "additions_only":
        return (list(diff.added), [])

    # strategy == "selected"
    if selected is None:
        raise ValueError("strategy='selected' requires a non-empty 'selected' set")
    selected_set = set(selected)
    upserts = sorted(set(diff.added + modified_names) & selected_set)
    deletes = sorted(set(diff.removed) & selected_set)
    return (upserts, deletes)


async def apply_diff(
    client: "ConfigServiceClient",
    diff: DeviceDiff,
    device_data: Dict[str, Dict[str, Any]],
    *,
    strategy: str,
    selected: Optional[Collection[str]] = None,
) -> Dict[str, List[str]]:
    """Apply the selected writes from ``diff`` to the config-service registry.

    Returns ``{"upserted": [...], "deleted": [...]}`` listing the names
    that were successfully written. Raises ``ConfigServiceError`` if any
    individual write fails, carrying a detail string that names every
    failure and chaining the first underlying exception via ``__cause__``
    (matches the no-silent-fallback policy used by ``_bootstrap_with_retry``).

    Writes inside each bucket run concurrently with
    ``return_exceptions=True`` so one slow/failing device doesn't stall
    the rest; if any failures remain after the gather, the function
    raises rather than reporting partial success. Upserts and deletes
    are issued in two passes (upserts first), so a rename-style change
    (delete + add a renamed device) lands "new device, then old removed"
    rather than briefly leaving the registry without the device.
    """
    upsert_names, delete_names = _select_writes(
        diff, strategy=strategy, selected=selected
    )

    async def _gather_with_failures(
        coros: Sequence[Tuple[str, Any]],
    ) -> Tuple[List[str], Dict[str, Exception]]:
        if not coros:
            return [], {}
        results = await asyncio.gather(
            *(coro for _, coro in coros), return_exceptions=True
        )
        successes: List[str] = []
        failures: Dict[str, Exception] = {}
        for (name, _), result in zip(coros, results):
            if isinstance(result, Exception):
                failures[name] = result
            elif isinstance(result, BaseException):
                # CancelledError / KeyboardInterrupt / SystemExit — propagate.
                raise result
            else:
                successes.append(name)
        return successes, failures

    upsert_coros: List[Tuple[str, Any]] = []
    for name in upsert_names:
        payload = device_data.get(name)
        if not isinstance(payload, dict) or "metadata" not in payload:
            raise ConfigServiceError(
                f"device-diff apply: worker payload for {name!r} is missing "
                "'metadata'; cannot upsert"
            )
        upsert_coros.append(
            (name, client.upsert_device(payload["metadata"], payload.get("spec")))
        )

    upserted, upsert_failures = await _gather_with_failures(upsert_coros)

    delete_coros = [(name, client.delete_device(name)) for name in delete_names]
    deleted, delete_failures = await _gather_with_failures(delete_coros)

    all_failures = {**upsert_failures, **delete_failures}
    if all_failures:
        ordered = sorted(all_failures.items())
        detail = ", ".join(
            f"{name}: {type(exc).__name__}: {exc}" for name, exc in ordered
        )
        first_cause = ordered[0][1]
        raise ConfigServiceError(
            "device-diff apply failed for "
            f"{len(all_failures)} device(s): {detail}"
        ) from first_cause

    return {"upserted": sorted(upserted), "deleted": sorted(deleted)}
