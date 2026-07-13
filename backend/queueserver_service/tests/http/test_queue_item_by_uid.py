"""In-process tests for ``GET /api/queue/item/{item_uid}``.

The frontend fetches a single queue item by UID as a path parameter
(``/queue/item/<uid>``); the existing ``/queue/item/get`` takes the UID in the
request body. This route serves the path-parameter form by delegating to the
same ``RM.item_get`` call. The ``:uuid`` path convertor keeps reserved literal
subpaths (``/queue/item/add`` etc.) routing to their own handlers.
"""

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from queueserver_service.http import authentication as auth
from queueserver_service.http.resources import SERVER_RESOURCES as SR
from queueserver_service.http.routers.queue import queue_router


class _StubRM:
    def __init__(self):
        self.calls = []

    async def item_get(self, *, uid=None, pos=None):
        self.calls.append({"uid": uid, "pos": pos})
        return {"success": True, "msg": "", "item": {"name": "count", "item_uid": uid}}


@pytest.fixture
def client_and_stub():
    """A TestClient over the queue router with auth bypassed and a stub RM.

    The global ``SERVER_RESOURCES`` singleton is restored afterwards so the stub
    never leaks into other tests sharing the process.
    """
    app = FastAPI()
    app.include_router(queue_router)
    app.dependency_overrides[auth.get_current_principal] = lambda: object()
    stub = _StubRM()
    original_rm = SR.RM
    SR.set_RM(stub)
    try:
        with TestClient(app) as client:
            yield client, stub
    finally:
        SR.set_RM(original_rm)


def test_get_queue_item_by_uid_delegates_to_item_get(client_and_stub):
    client, stub = client_and_stub
    item_uid = str(uuid.uuid4())
    resp = client.get(f"/api/queue/item/{item_uid}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["item"]["item_uid"] == item_uid
    # UID came from the path, not the body.
    assert stub.calls == [{"uid": item_uid, "pos": None}]


def test_literal_queue_item_get_is_not_shadowed(client_and_stub):
    client, stub = client_and_stub
    # "get" is not a UUID, so it hits the literal /queue/item/get route (uid from
    # body) rather than being captured by the path-parameter route.
    resp = client.request("GET", "/api/queue/item/get", json={"uid": "from-body"})
    assert resp.status_code == 200, resp.text
    assert stub.calls == [{"uid": "from-body", "pos": None}]


def test_non_uuid_segment_preserves_405_on_post_only_paths(client_and_stub):
    client, stub = client_and_stub
    # A GET to a POST-only literal must stay a 405, not be captured as
    # item_uid="move" and turned into a bogus item lookup.
    resp = client.get("/api/queue/item/move")
    assert resp.status_code == 405, resp.text
    assert stub.calls == []
