"""In-process tests for ``GET /api/queue/item/{item_uid}``.

The frontend fetches a single queue item by UID as a path parameter
(``/queue/item/<uid>``); the existing ``/queue/item/get`` takes the UID in the
request body. This route serves the path-parameter form by delegating to the
same ``RM.item_get`` call, and must not shadow the literal ``/queue/item/get``.
"""

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


def _client(stub):
    app = FastAPI()
    app.include_router(queue_router)
    app.dependency_overrides[auth.get_current_principal] = lambda: object()
    SR.set_RM(stub)
    return TestClient(app)


def test_get_queue_item_by_uid_delegates_to_item_get():
    stub = _StubRM()
    client = _client(stub)
    resp = client.get("/api/queue/item/abc-123")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["item"]["item_uid"] == "abc-123"
    # UID came from the path, not the body.
    assert stub.calls == [{"uid": "abc-123", "pos": None}]


def test_literal_queue_item_get_is_not_shadowed():
    stub = _StubRM()
    client = _client(stub)
    # "get" must hit the literal /queue/item/get route (uid from body), not be
    # captured as item_uid="get".
    resp = client.request("GET", "/api/queue/item/get", json={"uid": "from-body"})
    assert resp.status_code == 200, resp.text
    assert stub.calls == [{"uid": "from-body", "pos": None}]


def test_route_registration_order_guards_against_shadowing():
    paths = [
        getattr(r, "path", None)
        for r in queue_router.routes
        if "get" in {m.lower() for m in getattr(r, "methods", set())}
    ]
    literal = paths.index("/api/queue/item/get")
    param = paths.index("/api/queue/item/{item_uid}")
    assert literal < param, (
        "literal /queue/item/get must be registered before the {item_uid} route"
    )
