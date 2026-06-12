"""Unit tests for queueserver_service.manager.profile_collection.

These tests build real ephemeral git repositories under tmp_path and
exercise the async wrappers directly. No FastAPI or RE Manager required
— the HTTP layer is thin passthrough.

Tracking: NSLS2/ophyd-service#61.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from queueserver_service.manager.profile_collection import (
    PIXI_TOML_RELPATH,
    ProfileCollectionError,
    get_status,
    pull,
)


# ---------------------------------------------------------------------------
# Helpers — sync git plumbing for fixture setup. We do not exercise the
# module under test here; this is just stage-setting.
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    """Run git synchronously in cwd and return stdout (rstripped)."""
    env = os.environ.copy()
    # Deterministic commits regardless of the developer's git config.
    env.setdefault("GIT_AUTHOR_NAME", "Test")
    env.setdefault("GIT_AUTHOR_EMAIL", "test@example.invalid")
    env.setdefault("GIT_COMMITTER_NAME", "Test")
    env.setdefault("GIT_COMMITTER_EMAIL", "test@example.invalid")
    out = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.rstrip()


def _make_repo(tmp_path: Path, name: str = "repo") -> Path:
    """Initialize a fresh git repo with one commit on branch 'main'."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=main")
    (repo / "startup.py").write_text("# initial\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "initial")
    return repo


def _make_upstream_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    """Build a bare upstream + a clone tracking it. Returns (upstream, clone)."""
    seed = _make_repo(tmp_path, name="seed")
    upstream = tmp_path / "upstream.git"
    _git(seed.parent, "clone", "--quiet", "--bare", str(seed), str(upstream))
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "--quiet", str(upstream), str(clone))
    # Set local user inside the clone so commits made there work without
    # leaning on env vars on the second invocation.
    _git(clone, "config", "user.name", "Test")
    _git(clone, "config", "user.email", "test@example.invalid")
    return upstream, clone


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------


def test_get_status_rejects_unset_dir():
    with pytest.raises(ProfileCollectionError, match="not configured"):
        asyncio.run(get_status(None))


def test_get_status_rejects_missing_dir(tmp_path: Path):
    with pytest.raises(ProfileCollectionError, match="does not exist"):
        asyncio.run(get_status(str(tmp_path / "nope")))


def test_get_status_rejects_non_git_dir(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ProfileCollectionError, match="not a git checkout"):
        asyncio.run(get_status(str(plain)))


def test_get_status_clean_repo(tmp_path: Path):
    repo = _make_repo(tmp_path)
    status = asyncio.run(get_status(str(repo)))
    assert status.profile_dir == str(repo)
    assert len(status.commit) == 40
    assert status.branch == "main"
    assert status.is_dirty is False
    # No upstream configured on a bare init → ahead/behind both None.
    assert status.ahead is None
    assert status.behind is None


def test_get_status_dirty_untracked(tmp_path: Path):
    repo = _make_repo(tmp_path)
    (repo / "new.py").write_text("# untracked\n")
    status = asyncio.run(get_status(str(repo)))
    assert status.is_dirty is True


def test_get_status_dirty_modified(tmp_path: Path):
    repo = _make_repo(tmp_path)
    (repo / "startup.py").write_text("# modified\n")
    status = asyncio.run(get_status(str(repo)))
    assert status.is_dirty is True


def test_get_status_ahead_behind_with_upstream(tmp_path: Path):
    upstream, clone = _make_upstream_and_clone(tmp_path)

    # Fresh clone: 0/0.
    status = asyncio.run(get_status(str(clone)))
    assert status.ahead == 0
    assert status.behind == 0
    assert status.branch == "main"

    # Clone commits locally → 1 ahead, 0 behind.
    (clone / "local.py").write_text("# local\n")
    _git(clone, "add", ".")
    _git(clone, "commit", "--quiet", "-m", "local change")
    status = asyncio.run(get_status(str(clone)))
    assert status.ahead == 1
    assert status.behind == 0


# ---------------------------------------------------------------------------
# pull
# ---------------------------------------------------------------------------


def test_pull_rejects_dirty_tree(tmp_path: Path):
    _, clone = _make_upstream_and_clone(tmp_path)
    (clone / "startup.py").write_text("# unstaged edit\n")
    with pytest.raises(ProfileCollectionError, match="working tree is dirty"):
        asyncio.run(pull(str(clone)))


def test_pull_no_op_when_already_up_to_date(tmp_path: Path):
    _, clone = _make_upstream_and_clone(tmp_path)
    result = asyncio.run(pull(str(clone)))
    assert result.commit_before == result.commit_after
    assert result.files_changed == []
    assert result.pixi_toml_changed is False


def test_pull_fast_forwards_and_reports_diff(tmp_path: Path):
    upstream, clone = _make_upstream_and_clone(tmp_path)

    # Make a second clone, push a change, then pull into the first.
    sender = tmp_path / "sender"
    _git(tmp_path, "clone", "--quiet", str(upstream), str(sender))
    _git(sender, "config", "user.name", "Test")
    _git(sender, "config", "user.email", "test@example.invalid")
    (sender / "new_script.py").write_text("# added upstream\n")
    _git(sender, "add", ".")
    _git(sender, "commit", "--quiet", "-m", "added new_script.py")
    _git(sender, "push", "--quiet")

    result = asyncio.run(pull(str(clone)))
    assert result.commit_before != result.commit_after
    assert "new_script.py" in result.files_changed
    assert result.pixi_toml_changed is False


def test_pull_flags_pixi_toml_change(tmp_path: Path):
    upstream, clone = _make_upstream_and_clone(tmp_path)

    sender = tmp_path / "sender_pixi"
    _git(tmp_path, "clone", "--quiet", str(upstream), str(sender))
    _git(sender, "config", "user.name", "Test")
    _git(sender, "config", "user.email", "test@example.invalid")
    (sender / PIXI_TOML_RELPATH).write_text("[project]\nname='ios'\n")
    _git(sender, "add", ".")
    _git(sender, "commit", "--quiet", "-m", "add pixi.toml")
    _git(sender, "push", "--quiet")

    result = asyncio.run(pull(str(clone)))
    assert PIXI_TOML_RELPATH in result.files_changed
    assert result.pixi_toml_changed is True


def test_pull_rejects_non_fast_forward(tmp_path: Path):
    upstream, clone = _make_upstream_and_clone(tmp_path)

    # Make divergent histories: clone commits locally; another sender
    # pushes a different commit upstream. pull --ff-only should fail.
    (clone / "local_only.py").write_text("# local-only\n")
    _git(clone, "add", ".")
    _git(clone, "commit", "--quiet", "-m", "local-only commit")

    sender = tmp_path / "sender_div"
    _git(tmp_path, "clone", "--quiet", str(upstream), str(sender))
    _git(sender, "config", "user.name", "Test")
    _git(sender, "config", "user.email", "test@example.invalid")
    (sender / "remote_only.py").write_text("# remote-only\n")
    _git(sender, "add", ".")
    _git(sender, "commit", "--quiet", "-m", "remote-only commit")
    _git(sender, "push", "--quiet")

    with pytest.raises(ProfileCollectionError):
        asyncio.run(pull(str(clone)))
