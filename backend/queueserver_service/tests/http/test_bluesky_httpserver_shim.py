"""Tests for the ``bluesky_httpserver`` compatibility shim.

Upstream, the HTTP/WebSocket API server is the ``bluesky-httpserver`` distribution
(import package ``bluesky_httpserver``); in this repository it is implemented by
``queueserver_service.http``. This standalone shim distribution
(``backend/queueserver_service/bluesky-httpserver/``) must:

* provide the ``bluesky_httpserver`` import namespace, aliasing every submodule onto
  ``queueserver_service.http`` -- the SAME module object, not a re-executed copy (a
  second copy would fork settings singletons / DB engines and break ``isinstance``);
* resolve nested subpackages (``bluesky_httpserver.authorization.*``) and the
  config-driven ``module:object`` dotted paths beamline configs use;
* claim the ``bluesky-httpserver`` *distribution name* (pinned in lockstep with the
  implementation) without re-declaring the ``start-bluesky-httpserver`` console
  script the main distribution already owns; and
* stay lightweight on a bare ``import bluesky_httpserver`` (no FastAPI stack).

These tests require the ``bluesky-httpserver`` distribution to be installed
(``pip install -e ./bluesky-httpserver`` from ``backend/queueserver_service`` -- the
queueserver-tests CI job and the service Dockerfile both do this).
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from importlib import metadata

import pytest

# Upstream bluesky_httpserver submodules; each maps 1:1 onto queueserver_service.http.
SUBMODULES = [
    "app",
    "authentication",
    "authenticators",
    "authorization",
    "config",
    "config_schemas",
    "console_output",
    "core",
    "database",
    "resources",
    "routers",
    "schemas",
    "server",
    "settings",
    "utils",
]


def test_bare_import_is_lightweight():
    """A bare ``import bluesky_httpserver`` must not drag in the FastAPI application
    stack (or even ``queueserver_service``) -- mirroring upstream's __init__, which
    only computes ``__version__``. Run in a fresh interpreter so the assertion is not
    contaminated by modules other tests already imported."""
    code = (
        "import sys\n"
        "import bluesky_httpserver\n"
        "assert isinstance(bluesky_httpserver.__version__, str) and bluesky_httpserver.__version__\n"
        "heavy = [m for m in "
        "('fastapi', 'starlette', 'uvicorn', 'sqlalchemy', 'queueserver_service') "
        "if m in sys.modules]\n"
        "assert not heavy, f'bare import pulled in: {heavy}'\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


@pytest.mark.parametrize("sub", SUBMODULES)
def test_submodule_importable(sub):
    importlib.import_module(f"bluesky_httpserver.{sub}")  # must not raise


@pytest.mark.parametrize("sub", SUBMODULES)
def test_submodule_aliases_the_in_tree_module(sub):
    """Identity, not a copy: the shim submodule IS the queueserver_service.http one."""
    shim = importlib.import_module(f"bluesky_httpserver.{sub}")
    real = importlib.import_module(f"queueserver_service.http.{sub}")
    assert shim is real


def test_import_statement_for_app():
    """A literal ``import bluesky_httpserver.app`` (not just attribute access) works
    and yields the real module -- the case PEP-562 ``__getattr__`` alone can't cover."""
    import bluesky_httpserver.app  # noqa: F401
    import queueserver_service.http.app

    assert sys.modules["bluesky_httpserver.app"] is queueserver_service.http.app


def test_from_import_of_authenticator_class():
    from bluesky_httpserver.authenticators import DummyAuthenticator

    from queueserver_service.http.authenticators import DummyAuthenticator as RealDummy

    assert DummyAuthenticator is RealDummy


def test_server_entrypoints_importable():
    from bluesky_httpserver.server import app_factory, start_server

    assert callable(start_server)
    assert callable(app_factory)


def test_nested_subpackage_resolves_and_aliases():
    from bluesky_httpserver.authorization import BasicAPIAccessControl

    import bluesky_httpserver.authorization.api_access as shim_api_access
    import queueserver_service.http.authorization as real_authz
    import queueserver_service.http.authorization.api_access as real_api_access

    assert shim_api_access is real_api_access
    assert BasicAPIAccessControl is real_authz.BasicAPIAccessControl


def test_config_driven_dotted_path_load():
    """Beamline configs name authenticators / access-policy classes as
    ``module:object`` dotted paths; the fork resolves them via
    ``queueserver_service.http.utils.import_object``. The shim's dotted paths must
    resolve to the same objects the config machinery would load."""
    from queueserver_service.http.utils import import_object

    loaded = import_object("bluesky_httpserver.authenticators:DummyAuthenticator")
    policy = import_object("bluesky_httpserver.authorization:BasicAPIAccessControl")

    from queueserver_service.http.authenticators import DummyAuthenticator
    from queueserver_service.http.authorization import BasicAPIAccessControl

    assert loaded is DummyAuthenticator
    assert policy is BasicAPIAccessControl


def test_version_matches_distribution_metadata():
    import bluesky_httpserver

    assert bluesky_httpserver.__version__ == metadata.version("bluesky-httpserver")


def test_distribution_requires_the_implementation_in_lockstep():
    """The ``bluesky-httpserver`` dist must pin ``bluesky-queueserver`` exactly, so a
    version-skewed pair (the shim maps the impl's internal layout) is uninstallable."""
    requires = metadata.requires("bluesky-httpserver") or []
    pins = [r for r in requires if r.replace(" ", "").startswith("bluesky-queueserver==")]
    assert pins, f"expected an exact bluesky-queueserver== pin, got: {requires}"


def _console_scripts(dist_name):
    return {
        ep.name
        for ep in metadata.distribution(dist_name).entry_points
        if ep.group == "console_scripts"
    }


def test_console_script_owned_only_by_main_distribution():
    """``start-bluesky-httpserver`` must be provided solely by the ``bluesky-queueserver``
    distribution. Re-declaring it here would recreate the console-script clobber the
    shim exists to prevent."""
    assert "start-bluesky-httpserver" in _console_scripts("bluesky-queueserver")
    assert "start-bluesky-httpserver" not in _console_scripts("bluesky-httpserver")
