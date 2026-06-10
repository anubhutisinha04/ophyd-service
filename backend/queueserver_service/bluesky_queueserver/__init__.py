"""
Legacy import surface required by the PyPI ``bluesky-queueserver-api`` client.

The api client (a hard runtime dependency of ``queueserver_service.http``)
imports these names from ``bluesky_queueserver``; this distribution is also
named ``bluesky-queueserver`` so the client's ``Requires-Dist`` resolves to
this package instead of pulling the upstream distribution (which would shadow
the qserver/start-re-manager console scripts).

This module is ONLY the top-level legacy API. The implementation — and the
import path for all new code — is ``queueserver_service``; legacy subpaths
such as ``bluesky_queueserver.manager`` intentionally do not exist.
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
