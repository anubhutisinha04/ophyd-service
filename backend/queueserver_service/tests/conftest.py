import os
from pathlib import Path

# Several fixtures boot servers as subprocesses (start-re-manager workers,
# uvicorn via pytest-xprocess) and hand them dotted module paths from this
# test tree (tests.manager.spreadsheet_custom_functions,
# tests.http.access_api_server.api_server, ...). The test tree is not part of
# the installed package, so those processes can only import it if the service
# root is on their PYTHONPATH.
_service_root = str(Path(__file__).resolve().parent.parent)
os.environ["PYTHONPATH"] = os.pathsep.join(
    p for p in (_service_root, os.environ.get("PYTHONPATH")) if p
)
