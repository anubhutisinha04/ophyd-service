"""Profile-collection git operations for the UI-driven reload flow.

This module wraps `git status` / `git pull --ff-only` for the on-disk
profile collection that the RE Worker loads at environment-open time.
It is intentionally minimal: pure async subprocess calls against a
single directory, no manager state, no FastAPI imports. The HTTP layer
(``http/routers/profile_collection.py``) is the only caller.

Design notes:

- ``git pull`` is invoked with ``--ff-only``. Merges and rebases must
  be done by the operator, on-host, with a real shell. The endpoint's
  job is to roll forward clean working trees, not to mediate conflict
  resolution.
- Dirty working trees hard-block ``pull`` rather than silently stash.
  This is a deliberate policy choice documented in
  NSLS2/ophyd-service#61: operators must commit on-host edits before
  reloading. Silent stashing risks losing untracked work and surprising
  the next operator.
- ``pixi.toml`` changes are surfaced separately because re-materializing
  a conda env from a worker close+open is not possible. The HTTP layer
  uses this flag to 409 with ``requires_hard_restart: true`` rather
  than dragging the operator into an inconsistent state.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# Path to pixi manifest, relative to the profile-collection root. If this
# file appears in the pulled diff we surface a "hard restart required"
# signal — see ProfilePullResult.pixi_toml_changed.
PIXI_TOML_RELPATH = "pixi.toml"


class ProfileCollectionError(Exception):
    """Raised when a git operation against the profile collection fails."""


@dataclasses.dataclass(frozen=True)
class ProfileStatus:
    """Snapshot of the on-disk profile collection.

    ``commit`` is the HEAD SHA (40-char hex). ``branch`` is the local
    branch name, or ``None`` if HEAD is detached. ``is_dirty`` is True
    when ``git status --porcelain`` reports any output (staged,
    unstaged, or untracked). ``ahead``/``behind`` are commit counts
    against the configured upstream; both ``None`` when no upstream is
    set (e.g. fresh clone with detached HEAD).
    """

    profile_dir: str
    commit: str
    branch: Optional[str]
    is_dirty: bool
    ahead: Optional[int]
    behind: Optional[int]


@dataclasses.dataclass(frozen=True)
class ProfilePullResult:
    """Outcome of a ``git pull --ff-only`` invocation.

    ``commit_before`` and ``commit_after`` are the HEAD SHAs around the
    pull; equal when nothing changed. ``files_changed`` is the
    ``--name-only`` diff between the two commits, empty when no change.
    ``pixi_toml_changed`` flips True when ``PIXI_TOML_RELPATH`` is in
    ``files_changed``.
    """

    commit_before: str
    commit_after: str
    files_changed: List[str]
    pixi_toml_changed: bool


async def _run_git(
    args: List[str],
    cwd: str,
    *,
    check: bool = True,
) -> Tuple[int, str, str]:
    """Run ``git <args>`` in ``cwd`` and return (rc, stdout, stderr).

    Strings are decoded as UTF-8, with errors replaced. When
    ``check=True`` (default), a non-zero return code raises
    ``ProfileCollectionError`` with stderr in the message — the HTTP
    layer maps these to 4xx/5xx as appropriate.
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await proc.communicate()
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise ProfileCollectionError(
            f"git {' '.join(args)} (cwd={cwd}) exited {proc.returncode}: "
            f"{stderr.strip() or stdout.strip()}"
        )
    return proc.returncode or 0, stdout, stderr


def _validate_profile_dir(profile_dir: Optional[str]) -> str:
    """Resolve and sanity-check the profile-collection directory.

    Raises ``ProfileCollectionError`` if the path is unset, missing,
    or not a git checkout. Returns the absolute path on success.
    """
    if not profile_dir:
        raise ProfileCollectionError(
            "Profile-collection directory is not configured. Set "
            "QSERVER_HTTP_SERVER_PROFILE_COLLECTION_DIR or supply it in "
            "the HTTP server config."
        )
    abs_dir = os.path.abspath(profile_dir)
    if not os.path.isdir(abs_dir):
        raise ProfileCollectionError(
            f"Profile-collection directory {abs_dir!r} does not exist or is not a directory."
        )
    if not os.path.isdir(os.path.join(abs_dir, ".git")):
        raise ProfileCollectionError(
            f"Profile-collection directory {abs_dir!r} is not a git checkout "
            "(no .git/). The reload endpoints require git for safe "
            "version transitions."
        )
    return abs_dir


async def get_status(profile_dir: Optional[str]) -> ProfileStatus:
    """Inspect the profile collection without modifying it.

    Backs ``GET /api/profile_collection/status``. Cheap enough for the
    UI to poll (single ``rev-parse`` + ``status --porcelain`` +
    ``rev-list``).
    """
    abs_dir = _validate_profile_dir(profile_dir)

    _, commit_out, _ = await _run_git(["rev-parse", "HEAD"], abs_dir)
    commit = commit_out.strip()

    # symbolic-ref fails on detached HEAD; that's fine — surface as None.
    rc, branch_out, _ = await _run_git(
        ["symbolic-ref", "--quiet", "--short", "HEAD"], abs_dir, check=False
    )
    branch = branch_out.strip() if rc == 0 else None

    _, status_out, _ = await _run_git(["status", "--porcelain"], abs_dir)
    is_dirty = bool(status_out.strip())

    ahead: Optional[int] = None
    behind: Optional[int] = None
    rc, upstream_out, _ = await _run_git(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        abs_dir,
        check=False,
    )
    if rc == 0 and upstream_out.strip():
        rc, counts_out, _ = await _run_git(
            ["rev-list", "--left-right", "--count", "HEAD...@{upstream}"],
            abs_dir,
            check=False,
        )
        if rc == 0:
            try:
                a_str, b_str = counts_out.split()
                ahead = int(a_str)
                behind = int(b_str)
            except (ValueError, IndexError):
                # Unexpected git output shape; leave ahead/behind unset
                # rather than guess. Log and continue.
                logger.warning(
                    "rev-list returned unexpected counts %r for %s",
                    counts_out,
                    abs_dir,
                )

    return ProfileStatus(
        profile_dir=abs_dir,
        commit=commit,
        branch=branch,
        is_dirty=is_dirty,
        ahead=ahead,
        behind=behind,
    )


async def pull(profile_dir: Optional[str]) -> ProfilePullResult:
    """Fast-forward the profile collection from its upstream.

    Backs ``POST /api/profile_collection/pull``. Hard-rejects dirty
    working trees (``ProfileCollectionError`` — the HTTP layer maps to
    409). Runs ``git fetch`` then ``git merge --ff-only @{upstream}`` so
    we can report the precise file-list diff between the two SHAs.
    """
    abs_dir = _validate_profile_dir(profile_dir)

    # is_dirty check up front — never auto-stash.
    _, status_out, _ = await _run_git(["status", "--porcelain"], abs_dir)
    if status_out.strip():
        raise ProfileCollectionError(
            "Profile-collection working tree is dirty. Commit or revert "
            "on-host changes before pulling. (No silent stashing — see "
            "ophyd-service#61.)"
        )

    _, commit_before_out, _ = await _run_git(["rev-parse", "HEAD"], abs_dir)
    commit_before = commit_before_out.strip()

    # fetch first so we have an up-to-date @{upstream} to ff against.
    await _run_git(["fetch", "--quiet"], abs_dir)
    # --ff-only refuses to merge if upstream has diverged; surfaces as
    # ProfileCollectionError. Operator must resolve on-host.
    await _run_git(["merge", "--ff-only", "--quiet", "@{upstream}"], abs_dir)

    _, commit_after_out, _ = await _run_git(["rev-parse", "HEAD"], abs_dir)
    commit_after = commit_after_out.strip()

    files_changed: List[str] = []
    if commit_after != commit_before:
        _, diff_out, _ = await _run_git(
            ["diff", "--name-only", f"{commit_before}..{commit_after}"], abs_dir
        )
        files_changed = [line for line in diff_out.splitlines() if line.strip()]

    pixi_toml_changed = PIXI_TOML_RELPATH in files_changed

    return ProfilePullResult(
        commit_before=commit_before,
        commit_after=commit_after,
        files_changed=files_changed,
        pixi_toml_changed=pixi_toml_changed,
    )
