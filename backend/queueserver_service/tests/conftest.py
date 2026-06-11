import json
import os
from pathlib import Path

import pytest

# Several fixtures boot servers as subprocesses (start-re-manager workers,
# uvicorn via pytest-xprocess) and hand them dotted module paths from this
# test tree (tests.manager.spreadsheet_custom_functions,
# tests.http.access_api_server.api_server, ...). The test tree is not part of
# the installed package, so those processes can only import it if the service
# root is on their PYTHONPATH.
_service_root = Path(__file__).resolve().parent.parent
os.environ["PYTHONPATH"] = os.pathsep.join(
    p for p in (str(_service_root), os.environ.get("PYTHONPATH")) if p
)

# Tests at or above this recorded duration get the ``slow`` marker, so
# ``pytest -m "not slow"`` is a fast development loop (~12 min vs ~1.5 h for
# the full suite at the default 2 s). Durations come from the committed
# pytest-split data; tests without a recorded duration count as fast.
_SLOW_THRESHOLD_S = float(os.environ.get("QS_SLOW_TEST_THRESHOLD", "2"))
_DURATIONS_FILE = _service_root / ".test_durations"


def _base_id(nodeid):
    return nodeid.split("[", 1)[0]


def pytest_collection_modifyitems(config, items):
    try:
        durations = json.loads(_DURATIONS_FILE.read_text())
    except (OSError, ValueError):
        return
    # Recorded ids go stale when a test's parametrization changes, so fall
    # back to the slowest recorded variant of the same test function; better
    # to over-mark a fast variant slow than to let a slow one into the fast
    # tier.
    by_base = {}
    for nodeid, seconds in durations.items():
        base = _base_id(nodeid)
        by_base[base] = max(by_base.get(base, 0.0), seconds)
    slow = pytest.mark.slow
    for item in items:
        recorded = durations.get(item.nodeid)
        if recorded is None:
            recorded = by_base.get(_base_id(item.nodeid), 0.0)
        if recorded >= _SLOW_THRESHOLD_S:
            item.add_marker(slow)
