"""Tests for the ``bluesky_queueserver`` compatibility shim.

This distribution is named ``bluesky-queueserver`` and must provide the legacy
import surface that the ``bluesky-queueserver-api`` client and existing beamline
startup scripts (and upstream sample profiles) rely on:

* the top-level names re-exported from ``bluesky_queueserver``, and
* the ``bluesky_queueserver.manager.*`` subpaths that real profiles import,
  e.g. ``from bluesky_queueserver.manager.profile_tools import set_user_ns``.
"""

from __future__ import annotations

import importlib

import pytest

# Top-level names re-exported by the shim (matches upstream bluesky_queueserver
# 0.0.24 export list; '__version__' included).
EXPECTED_TOP_LEVEL = [
    "__version__",
    "CommTimeoutError",
    "ReceiveConsoleOutput",
    "ReceiveConsoleOutputAsync",
    "ReceiveSystemInfo",
    "ReceiveSystemInfoAsync",
    "ZMQCommSendAsync",
    "ZMQCommSendThreads",
    "bind_plan_arguments",
    "clear_ipython_mode",
    "clear_re_worker_active",
    "construct_parameters",
    "format_text_descriptions",
    "gen_list_of_plans_and_devices",
    "generate_zmq_keys",
    "generate_zmq_public_key",
    "is_ipython_mode",
    "is_re_worker_active",
    "parameter_annotation_decorator",
    "register_device",
    "register_plan",
    "set_ipython_mode",
    "set_re_worker_active",
    "validate_plan",
    "validate_zmq_key",
]


def test_top_level_shim_exports_full_list():
    mod = importlib.import_module("bluesky_queueserver")
    missing = [name for name in EXPECTED_TOP_LEVEL if not hasattr(mod, name)]
    assert not missing, f"bluesky_queueserver shim missing names: {missing}"


@pytest.mark.parametrize(
    "subpath",
    [
        "bluesky_queueserver.manager",
        "bluesky_queueserver.manager.profile_tools",
        "bluesky_queueserver.manager.annotation_decorator",
        "bluesky_queueserver.manager.profile_ops",
    ],
)
def test_manager_subpaths_importable(subpath):
    importlib.import_module(subpath)  # must not raise ModuleNotFoundError


def test_manager_subpaths_alias_the_in_tree_modules():
    import queueserver_service.manager as real_manager
    import queueserver_service.manager.annotation_decorator as real_ad
    import queueserver_service.manager.profile_ops as real_po
    import queueserver_service.manager.profile_tools as real_pt

    shim_manager = importlib.import_module("bluesky_queueserver.manager")
    shim_pt = importlib.import_module("bluesky_queueserver.manager.profile_tools")
    shim_ad = importlib.import_module("bluesky_queueserver.manager.annotation_decorator")
    shim_po = importlib.import_module("bluesky_queueserver.manager.profile_ops")

    assert shim_manager is real_manager
    assert shim_pt is real_pt
    assert shim_ad is real_ad
    assert shim_po is real_po


def test_documented_beamline_profile_imports_work():
    # The exact idioms used by upstream sample profiles / beamline startup code.
    from bluesky_queueserver.manager.annotation_decorator import parameter_annotation_decorator
    from bluesky_queueserver.manager.profile_tools import (
        global_user_namespace,
        load_devices_from_happi,
        set_user_ns,
    )

    assert callable(set_user_ns)
    assert callable(load_devices_from_happi)
    assert callable(parameter_annotation_decorator)
    assert global_user_namespace is not None


def test_zmq_era_subpaths_are_not_aliased():
    """The 0MQ-era subpaths are deliberately NOT provided (that surface is being
    retired); guard against accidentally re-adding them."""
    for subpath in (
        "bluesky_queueserver.manager.comms",
        "bluesky_queueserver.manager.json_rpc",
        "bluesky_queueserver.manager.logging_setup",
    ):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(subpath)
