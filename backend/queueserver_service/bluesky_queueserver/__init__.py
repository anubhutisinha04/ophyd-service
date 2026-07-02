"""
Legacy import surface required by the PyPI ``bluesky-queueserver-api`` client.

The api client (a hard runtime dependency of ``queueserver_service.http``)
imports these names from ``bluesky_queueserver``; this distribution is also
named ``bluesky-queueserver`` so the client's ``Requires-Dist`` resolves to
this package instead of pulling the upstream distribution (which would shadow
the qserver/start-re-manager console scripts).

The implementation — and the import path for all new code — is
``queueserver_service``. For backward compatibility with existing beamline
startup scripts and upstream's own sample profiles, a small set of legacy
``bluesky_queueserver.manager.*`` subpaths is aliased to the in-tree
implementation at the bottom of this module (see there for the exact set, and
why the ZeroMQ-layer ``.comms`` / ``.json_rpc`` / ``.logging_setup`` subpaths
are deliberately excluded).
"""

from queueserver_service import __version__  # noqa: F401
from queueserver_service import (  # noqa: F401
    CommTimeoutError,
    ReceiveConsoleOutput,
    ReceiveConsoleOutputAsync,
    ReceiveSystemInfo,
    ReceiveSystemInfoAsync,
    ZMQCommSendAsync,
    ZMQCommSendThreads,
    bind_plan_arguments,
    clear_ipython_mode,
    clear_re_worker_active,
    construct_parameters,
    format_text_descriptions,
    gen_list_of_plans_and_devices,
    generate_zmq_keys,
    generate_zmq_public_key,
    is_ipython_mode,
    is_re_worker_active,
    parameter_annotation_decorator,
    register_device,
    register_plan,
    set_ipython_mode,
    set_re_worker_active,
    validate_plan,
    validate_zmq_key,
)

# ---------------------------------------------------------------------------
# Legacy ``bluesky_queueserver.manager.*`` subpath aliases.
#
# Real beamline startup scripts and upstream's own sample profiles import from
# these subpaths, e.g.::
#
#     from bluesky_queueserver.manager.profile_tools import set_user_ns, load_devices_from_happi
#     from bluesky_queueserver.manager.annotation_decorator import parameter_annotation_decorator
#
# Because this distribution shadows upstream ``bluesky-queueserver`` (it is
# named ``bluesky-queueserver``), such profiles would fail to load without these
# aliases and users can't install upstream alongside. Map the dotted names to
# the in-tree modules; these are already imported by ``queueserver_service``
# above, so aliasing adds no new import cost.
#
# The ``.comms`` / ``.json_rpc`` / ``.logging_setup`` subpaths are intentionally
# NOT aliased: they belong to the ZeroMQ messaging layer, which this service does
# not expose to external importers.
# ---------------------------------------------------------------------------
import sys  # noqa: E402

import queueserver_service.manager  # noqa: E402
import queueserver_service.manager.annotation_decorator  # noqa: E402
import queueserver_service.manager.profile_ops  # noqa: E402
import queueserver_service.manager.profile_tools  # noqa: E402

sys.modules["bluesky_queueserver.manager"] = queueserver_service.manager
sys.modules["bluesky_queueserver.manager.profile_tools"] = queueserver_service.manager.profile_tools
sys.modules["bluesky_queueserver.manager.annotation_decorator"] = (
    queueserver_service.manager.annotation_decorator
)
sys.modules["bluesky_queueserver.manager.profile_ops"] = queueserver_service.manager.profile_ops
