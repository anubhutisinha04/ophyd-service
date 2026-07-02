"""Compatibility shim providing the ``bluesky_httpserver`` import surface.

Upstream, the Bluesky HTTP/WebSocket API server is the ``bluesky-httpserver``
distribution (import package ``bluesky_httpserver``). In this repository the
implementation lives in :mod:`queueserver_service.http`; this thin package (the
``bluesky-httpserver`` distribution -- see the sibling ``pyproject.toml``) exists
so that:

* existing deployments and beamline startup scripts that ``import
  bluesky_httpserver`` (or its submodules, e.g.
  ``from bluesky_httpserver.server import start_server`` or a uvicorn
  ``--factory bluesky_httpserver.server:app_factory``) keep working, and
* the ``bluesky-httpserver`` *distribution name* is claimed by this fork, so a
  third-party ``Requires-Dist: bluesky-httpserver`` (or an explicit
  ``pip install bluesky-httpserver``) resolves here instead of pulling the
  upstream distribution -- which would collide on this import package and clobber
  the ``start-bluesky-httpserver`` console script.

Every upstream ``bluesky_httpserver`` submodule maps 1:1 onto
``queueserver_service.http`` (``server``, ``config``, ``settings``,
``authentication``, ``authenticators``, ``authorization``, ``core``,
``schemas``, ``resources``, ``console_output``, ``utils``, ``app``, ``routers``,
``database``, ``config_schemas``). Rather than eagerly importing all of them --
which would pull the entire FastAPI application stack on a bare
``import bluesky_httpserver`` -- submodules are resolved lazily by the meta-path
finder installed below, so ``bluesky_httpserver.<name>`` transparently *is*
``queueserver_service.http.<name>`` (same module object) but is only imported on
first use. This mirrors upstream's own ``__init__``, which exposes only
``__version__``.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

_SHIM_PREFIX = "bluesky_httpserver."
_TARGET_PREFIX = "queueserver_service.http."

# Keep a bare ``import bluesky_httpserver`` lightweight (like upstream's
# ``__version__``-only ``__init__``): read the version from installed distribution
# metadata rather than importing ``queueserver_service``, so the FastAPI application
# stack is pulled in only when a submodule is actually used. The shim and its
# implementation are pinned in lockstep (see pyproject.toml), so either name yields
# the same version.
try:
    __version__ = _dist_version("bluesky-httpserver")
except PackageNotFoundError:  # pragma: no cover - source tree without dist metadata
    try:
        __version__ = _dist_version("bluesky-queueserver")
    except PackageNotFoundError:
        __version__ = "0+unknown"


class _HttpserverShimLoader(importlib.abc.Loader):
    """Rebind a ``bluesky_httpserver.<sub>`` name to its real target module.

    ``exec_module`` replaces the throwaway module the import system created for
    the shim name with the already-initialized ``queueserver_service.http.<sub>``
    module, so the two names refer to the exact same object (identity is
    preserved) without ever clobbering the real module's ``__name__``/``__spec__``.
    """

    def __init__(self, target_name):
        self._target_name = target_name

    def create_module(self, spec):
        return None  # use the default module object; it is replaced in exec_module

    def exec_module(self, module):
        # Alias, do NOT re-execute. ``import_module`` returns the already-imported
        # (cached) real module; rebinding the shim name to that exact object makes
        # ``bluesky_httpserver.X is queueserver_service.http.X`` -- a single instance,
        # so settings singletons, DB engines and ``isinstance`` checks stay consistent
        # across both import paths. (Replacing a module in ``sys.modules`` during
        # ``exec_module`` is an import-system-supported pattern.)
        sys.modules[module.__name__] = importlib.import_module(self._target_name)


class _HttpserverShimFinder(importlib.abc.MetaPathFinder):
    """Map ``bluesky_httpserver.<sub>`` imports onto ``queueserver_service.http.<sub>``."""

    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(_SHIM_PREFIX):
            return None
        target_name = _TARGET_PREFIX + fullname[len(_SHIM_PREFIX) :]
        return importlib.util.spec_from_loader(fullname, _HttpserverShimLoader(target_name))


if not any(isinstance(finder, _HttpserverShimFinder) for finder in sys.meta_path):
    sys.meta_path.append(_HttpserverShimFinder())
