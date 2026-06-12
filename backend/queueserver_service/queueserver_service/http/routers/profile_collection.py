"""HTTP routes for the UI-driven profile-collection reload flow.

Implements the design from NSLS2/ophyd-service#61. Three endpoints are
active in this PR:

- ``GET  /api/profile_collection/status`` — cheap status poll.
- ``POST /api/profile_collection/pull``   — fast-forward git pull.
- ``POST /api/profile_collection/reload`` — wrapper: pull → close → open.

Two endpoints are present as 501 stubs because their bodies need a
``compute_diff`` / ``apply_diff`` split inside
``manager/config_service.py`` that collides with an in-flight PR (#59).
They activate in a follow-up commit on this branch once #59 merges:

- ``GET  /api/devices/diff_against_profile``
- ``POST /api/devices/sync_from_profile``

The profile-collection directory is supplied via the
``QSERVER_HTTP_SERVER_PROFILE_COLLECTION_DIR`` environment variable.
This is intentionally distinct from the manager's ``startup_dir`` (the
manager and HTTP server are separate processes and the HTTP server has
no ZMQ accessor for the manager's path today). For the IOS deployment,
the ansible role sets both to the same path —
``/opt/ophyd-service/profile_collection``.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Security
from pydantic import BaseModel, Field

from ...manager.profile_collection import (
    ProfileCollectionError,
    get_status,
    pull,
)
from ..authentication import get_current_principal
from ..resources import SERVER_RESOURCES as SR
from ..settings import get_settings
from ..utils import process_exception

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/profile_collection", tags=["Profile Collection"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class ProfileStatusResponse(BaseModel):
    """Status of the on-disk profile collection."""

    profile_dir: str = Field(..., description="Absolute path on the server.")
    commit: str = Field(..., description="HEAD commit SHA (40-char hex).")
    branch: Optional[str] = Field(
        None,
        description="Current branch name, or null if HEAD is detached.",
    )
    is_dirty: bool = Field(
        ...,
        description=(
            "True when the working tree has uncommitted changes "
            "(staged, unstaged, or untracked)."
        ),
    )
    ahead: Optional[int] = Field(
        None,
        description=(
            "Commits ahead of the configured upstream, or null if no "
            "upstream is set."
        ),
    )
    behind: Optional[int] = Field(
        None,
        description=(
            "Commits behind the configured upstream, or null if no "
            "upstream is set."
        ),
    )


class ProfilePullResponse(BaseModel):
    """Outcome of a successful fast-forward pull."""

    commit_before: str = Field(..., description="HEAD SHA before the pull.")
    commit_after: str = Field(..., description="HEAD SHA after the pull.")
    files_changed: List[str] = Field(
        default_factory=list,
        description=(
            "Files that differ between commit_before and commit_after. "
            "Empty list when the pull was a no-op."
        ),
    )
    pixi_toml_changed: bool = Field(
        ...,
        description=(
            "True when pixi.toml is in files_changed. The HTTP layer "
            "uses this to surface requires_hard_restart to the UI; the "
            "endpoint itself does not 409 because the operator may "
            "have intentionally pulled without an open environment."
        ),
    )


class ProfileReloadResponse(BaseModel):
    """Outcome of the wrapper that pulls + recycles the worker."""

    pull: ProfilePullResponse
    environment_recycled: bool = Field(
        ...,
        description=(
            "True if an existing environment was closed and reopened. "
            "False when no environment was open at call time — the "
            "operator must call /api/environment/open separately to "
            "trigger device introspection."
        ),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolved_profile_dir() -> Optional[str]:
    """Return the configured profile-collection directory.

    Stub indirection so a future settings-driven override can plug in
    here without touching every handler. Today it reads the
    ``profile_collection_dir`` attribute off the cached Settings if
    present, falling back to ``None`` — ``_validate_profile_dir`` in
    the manager module raises a clear error in that case.
    """
    return getattr(get_settings(), "profile_collection_dir", None)


def _map_profile_error(exc: ProfileCollectionError) -> HTTPException:
    """Map a ProfileCollectionError to the right HTTP status.

    Configuration errors (no dir, not a git checkout) → 500: the
    operator can't fix these from the UI. Dirty-tree and non-ff merge
    errors → 409: the operator can fix these on-host. Anything else is
    a generic 500 with the underlying message.
    """
    msg = str(exc)
    if "is not configured" in msg or "does not exist" in msg or "not a git checkout" in msg:
        return HTTPException(status_code=500, detail=msg)
    if "working tree is dirty" in msg or "--ff-only" in msg or "ff-only" in msg:
        return HTTPException(status_code=409, detail=msg)
    return HTTPException(status_code=500, detail=msg)


# ---------------------------------------------------------------------------
# Active endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/status",
    response_model=ProfileStatusResponse,
    summary="Status of the on-disk profile collection",
    description=(
        "Returns the current commit, branch, dirty flag, and "
        "ahead/behind counts relative to the configured upstream. "
        "Cheap; safe to poll from the UI for the Profile Collection "
        "header badge. Required scope: `read:status`."
    ),
)
async def profile_collection_status_handler(
    principal=Security(get_current_principal, scopes=["read:status"]),
) -> ProfileStatusResponse:
    try:
        status = await get_status(_resolved_profile_dir())
    except ProfileCollectionError as exc:
        raise _map_profile_error(exc)
    return ProfileStatusResponse(
        profile_dir=status.profile_dir,
        commit=status.commit,
        branch=status.branch,
        is_dirty=status.is_dirty,
        ahead=status.ahead,
        behind=status.behind,
    )


@router.post(
    "/pull",
    response_model=ProfilePullResponse,
    summary="Fast-forward the profile collection from upstream",
    description=(
        "Runs `git fetch` then `git merge --ff-only @{upstream}` in "
        "the profile-collection directory. Rejected (409) when the "
        "working tree is dirty — operator must commit or revert "
        "on-host edits first. Rejected (409) when upstream has "
        "diverged (non-fast-forward) — operator must resolve on-host. "
        "Returns commit_before, commit_after, the diff file list, and "
        "a pixi_toml_changed flag the UI uses to decide whether a "
        "hard service restart is needed before reload. Required "
        "scope: `write:manager:control`."
    ),
)
async def profile_collection_pull_handler(
    principal=Security(get_current_principal, scopes=["write:manager:control"]),
) -> ProfilePullResponse:
    try:
        result = await pull(_resolved_profile_dir())
    except ProfileCollectionError as exc:
        raise _map_profile_error(exc)
    return ProfilePullResponse(
        commit_before=result.commit_before,
        commit_after=result.commit_after,
        files_changed=result.files_changed,
        pixi_toml_changed=result.pixi_toml_changed,
    )


@router.post(
    "/reload",
    response_model=ProfileReloadResponse,
    summary="Pull and recycle the worker (one-shot for the UI)",
    description=(
        "Wrapper that runs `pull`, then — if an environment is open — "
        "closes and reopens it so the new profile is loaded into the "
        "worker. Does NOT mutate the device registry; the UI should "
        "follow up with `GET /api/devices/diff_against_profile` and "
        "(on operator confirm) `POST /api/devices/sync_from_profile`. "
        "Returns 409 when the working tree is dirty or when "
        "pixi.toml changed and an environment is currently open "
        "(pixi.toml changes require a service-level restart — see "
        "ophyd-service#61). Required scope: `write:manager:control`."
    ),
)
async def profile_collection_reload_handler(
    principal=Security(get_current_principal, scopes=["write:manager:control"]),
) -> ProfileReloadResponse:
    try:
        pull_result = await pull(_resolved_profile_dir())
    except ProfileCollectionError as exc:
        raise _map_profile_error(exc)

    # Snapshot whether an environment is currently open. If yes,
    # close+open so the new profile is loaded. The pixi.toml-changed
    # branch above is checked here because we have to know if env is
    # open to decide whether the operator has to restart the service.
    try:
        status_msg = await SR.RM.status()
    except Exception:
        process_exception()

    env_exists = bool(status_msg.get("worker_environment_exists", False))

    if pull_result.pixi_toml_changed and env_exists:
        raise HTTPException(
            status_code=409,
            detail=(
                "pixi.toml changed and an environment is currently open. "
                "A worker recycle cannot re-materialize the conda env; "
                "the service must be restarted on-host (see "
                "ophyd-service#61). The pull has already been applied "
                "to the on-disk profile."
            ),
        )

    environment_recycled = False
    if env_exists:
        try:
            await SR.RM.environment_close()
            await SR.RM.environment_open()
            environment_recycled = True
        except Exception:
            process_exception()

    return ProfileReloadResponse(
        pull=ProfilePullResponse(
            commit_before=pull_result.commit_before,
            commit_after=pull_result.commit_after,
            files_changed=pull_result.files_changed,
            pixi_toml_changed=pull_result.pixi_toml_changed,
        ),
        environment_recycled=environment_recycled,
    )


# ---------------------------------------------------------------------------
# Stub endpoints — bodies arrive in a follow-up commit (depends on #59)
# ---------------------------------------------------------------------------


# A second router for the /api/devices stubs so the prefix differs from
# the /api/profile_collection router above. Same module; same tag for
# OpenAPI grouping convenience.
devices_router = APIRouter(prefix="/api/devices", tags=["Profile Collection"])


@devices_router.get(
    "/diff_against_profile",
    summary="Diff worker-introspected devices against the registry (stub)",
    description=(
        "Will return added/removed/modified device specs vs "
        "configuration_service. Non-destructive. Backed by a "
        "compute_diff helper that this PR cannot land yet because the "
        "split inside manager/config_service.py collides with #59. "
        "Returns 501 until the follow-up commit on this branch lands. "
        "See ophyd-service#61."
    ),
)
async def devices_diff_against_profile_stub(
    principal=Security(get_current_principal, scopes=["read:status"]),
):
    raise HTTPException(
        status_code=501,
        detail=(
            "Not implemented yet. Requires the compute_diff/apply_diff "
            "split in manager/config_service.py — landing in a follow-up "
            "commit once ophyd-service#59 merges. Tracking: "
            "ophyd-service#61."
        ),
    )


@devices_router.post(
    "/sync_from_profile",
    summary="Apply a selected/all device diff to the registry (stub)",
    description=(
        "Will accept {strategy: 'all'|'additions_only'|'selected', "
        "devices?: [...]} and upsert/remove accordingly. Returns 501 "
        "until the follow-up commit on this branch lands. See "
        "ophyd-service#61."
    ),
)
async def devices_sync_from_profile_stub(
    principal=Security(get_current_principal, scopes=["write:manager:control"]),
):
    raise HTTPException(
        status_code=501,
        detail=(
            "Not implemented yet. Requires the compute_diff/apply_diff "
            "split in manager/config_service.py — landing in a follow-up "
            "commit once ophyd-service#59 merges. Tracking: "
            "ophyd-service#61."
        ),
    )
