import json
import pprint
import threading
import time as ttime

import pytest
from starlette.websockets import WebSocket
from tests.manager.common import re_manager, re_manager_cmd, re_manager_factory  # noqa F401
from websockets.sync.client import connect

from .conftest import fastapi_server_fs  # noqa: F401
from .conftest import (
    SERVER_ADDRESS,
    SERVER_PORT,
    request_to_json,
    setup_server_with_config_file,
    wait_for_environment_to_be_closed,
    wait_for_environment_to_be_created,
)

config_toy_test = """
authentication:
    allow_anonymous_access: True
    providers:
        - provider: toy
          authenticator: queueserver_service.http.authenticators:DictionaryAuthenticator
          args:
              users_to_passwords:
                  bob: bob_password
                  alice: alice_password
                  cara: cara_password
                  tom: tom_password
api_access:
  policy: queueserver_service.http.authorization:DictionaryAPIAccessControl
  args:
    users:
      bob:
        roles:
          - admin
          - expert
      alice:
        roles: advanced
      tom:
        roles: user
"""


class _ReceiveSystemInfoSocket(threading.Thread):
    """
    Catch streaming console output by connecting to /console_output/ws socket and
    save messages to the buffer.
    """

    def __init__(self, *, endpoint, api_key=None, token=None, **kwargs):
        super().__init__(**kwargs)
        self.received_data_buffer = []
        self._exit = False
        self._api_key = api_key
        self._token = token
        self._endpoint = endpoint

    def run(self):
        websocket_uri = f"ws://{SERVER_ADDRESS}:{SERVER_PORT}/api{self._endpoint}"
        if self._token is not None:
            additional_headers = {"Authorization": f"Bearer {self._token}"}
        elif self._api_key is not None:
            additional_headers = {"Authorization": f"ApiKey {self._api_key}"}
        else:
            additional_headers = {}

        try:
            with connect(websocket_uri, additional_headers=additional_headers) as websocket:
                while not self._exit:
                    try:
                        msg_json = websocket.recv(timeout=0.1, decode=False)
                        try:
                            msg = json.loads(msg_json)
                            self.received_data_buffer.append(msg)
                        except json.JSONDecodeError:
                            pass
                    except TimeoutError:
                        pass
        except Exception as ex:
            print(f"Failed to connect to server: {ex}")

    def stop(self):
        """
        Call this method to stop the thread. Then send a request to the server so that some output
        is printed in ``stdout``.
        """
        self._exit = True

    def __del__(self):
        self.stop()


# fmt: off
@pytest.mark.parametrize("ws_auth_type", ["apikey", "apikey_invalid", "none"])
# fmt: on
def test_websocket_auth_01(
    tmpdir,
    monkeypatch,
    re_manager_cmd,  # noqa: F811
    fastapi_server_fs,  # noqa: F811
    ws_auth_type,
):
    """
    Test authentication for websockets. The test is run only on ``/status/ws`` websocket.
    The other websockets are expected to use the same authentication scheme.
    """

    # Start RE Manager
    params = ["--zmq-publish-console", "ON"]
    re_manager_cmd(params)

    setup_server_with_config_file(config_file_str=config_toy_test, tmpdir=tmpdir, monkeypatch=monkeypatch)
    fastapi_server_fs()

    resp1 = request_to_json("post", "/auth/provider/toy/token", login=("bob", "bob_password"))
    assert "access_token" in pprint.pformat(resp1)
    token = resp1["access_token"]

    resp3 = request_to_json(
        "post", "/auth/apikey", json={"expires_in": 900, "note": "API key for testing"}, token=token
    )
    assert "secret" in resp3, pprint.pformat(resp3)
    assert "note" in resp3, pprint.pformat(resp3)
    assert resp3["note"] == "API key for testing"
    assert resp3["scopes"] == ["inherit"]
    api_key = resp3["secret"]

    endpoint = "/status/ws"
    if ws_auth_type == "none":
        ws_params = {}
    elif ws_auth_type == "apikey":
        ws_params = {"api_key": api_key}
    elif ws_auth_type == "apikey_invalid":
        ws_params = {"api_key": "InvalidApiKey"}
    # elif ws_auth_type == "token":
    #     ws_params = {"token": token}
    # elif ws_auth_type == "token_invalid":
    #     ws_params = {"token": "InvalidToken"}
    else:
        assert False, f"Unknown authentication type: {ws_auth_type!r}"

    rsc = _ReceiveSystemInfoSocket(endpoint=endpoint, **ws_params)
    rsc.start()
    ttime.sleep(1)  # Wait until the client connects to the socket

    resp1 = request_to_json("post", "/environment/open", api_key=api_key)
    assert resp1["success"] is True, pprint.pformat(resp1)

    assert wait_for_environment_to_be_created(timeout=10, api_key=api_key)

    resp2b = request_to_json("post", "/environment/close", api_key=api_key)
    assert resp2b["success"] is True, pprint.pformat(resp2b)

    assert wait_for_environment_to_be_closed(timeout=10, api_key=api_key)

    # Wait until capture is complete
    ttime.sleep(2)
    rsc.stop()
    rsc.join()

    buffer = rsc.received_data_buffer
    if ws_auth_type in ("none", "apikey_invalid", "token_invalid"):
        assert len(buffer) == 0
    elif ws_auth_type in ("apikey", "token"):
        assert len(buffer) > 0
        for msg in buffer:
            assert "time" in msg, msg
            assert isinstance(msg["time"], float), msg
            assert "msg" in msg
            assert isinstance(msg["msg"], dict)
    else:
        assert False, f"Unknown authentication type: {ws_auth_type!r}"


def _make_websocket(app, *, query_string=b"", headers=None):
    """Build a minimal Starlette WebSocket for a /status/ws handshake scope."""
    scope = {
        "type": "websocket",
        "path": "/api/status/ws",
        "query_string": query_string,
        "headers": headers or [],
        "app": app,
    }
    return WebSocket(scope, receive=None, send=None)


# fmt: off
@pytest.mark.parametrize("scheme", ["ApiKey", "Apikey", "apikey", "aPiKeY", "APIKEY"])
# fmt: on
def test_websocket_apikey_header_case_insensitive(monkeypatch, scheme):
    """
    The API-key scheme name in the WebSocket 'Authorization' header must be accepted
    case-insensitively, matching the HTTP path — a client's header casing must not
    decide whether a WebSocket authenticates.
    """
    from queueserver_service.http import authentication as auth

    captured = {}

    def _fake_get_current_principal(*, api_key, access_token, **kwargs):
        captured["api_key"] = api_key
        captured["access_token"] = access_token
        return object() if api_key else None

    monkeypatch.setattr(auth, "get_current_principal", _fake_get_current_principal)

    class _App:
        dependency_overrides = {
            auth.get_settings: lambda: object(),
            auth.get_authenticators: lambda: {},
            auth.get_api_access_manager: lambda: object(),
        }

    ws = _make_websocket(_App(), headers=[(b"authorization", f"{scheme} SECRET".encode())])
    principal = auth.get_current_principal_websocket(websocket=ws, scopes=["read:monitor"])
    assert captured["api_key"] == "SECRET"
    assert principal is not None


def test_websocket_apikey_query_and_precedence(monkeypatch):
    """
    The API key may also be supplied as an '?api_key=' query parameter (matching the
    HTTP path). The 'Authorization' header takes precedence when both are present.
    A 'Bearer' token is not accepted as an API key.
    """
    from queueserver_service.http import authentication as auth

    captured = {}

    def _fake_get_current_principal(*, api_key, access_token, **kwargs):
        captured["api_key"] = api_key
        captured["access_token"] = access_token
        return object() if api_key else None

    monkeypatch.setattr(auth, "get_current_principal", _fake_get_current_principal)

    class _App:
        dependency_overrides = {
            auth.get_settings: lambda: object(),
            auth.get_authenticators: lambda: {},
            auth.get_api_access_manager: lambda: object(),
        }

    app = _App()

    # Key from the query parameter (no Authorization header).
    ws = _make_websocket(app, query_string=b"api_key=QUERYKEY")
    assert auth.get_current_principal_websocket(websocket=ws, scopes=["read:monitor"]) is not None
    assert captured["api_key"] == "QUERYKEY"

    # Header wins over query when both are present.
    ws = _make_websocket(
        app, query_string=b"api_key=QUERYKEY", headers=[(b"authorization", b"ApiKey HEADERKEY")]
    )
    auth.get_current_principal_websocket(websocket=ws, scopes=["read:monitor"])
    assert captured["api_key"] == "HEADERKEY"

    # A Bearer token is not treated as an API key (bearer auth is unsupported here).
    ws = _make_websocket(app, headers=[(b"authorization", b"Bearer SOME.JWT.TOKEN")])
    assert auth.get_current_principal_websocket(websocket=ws, scopes=["read:monitor"]) is None
    assert captured["api_key"] is None
    assert captured["access_token"] is None

    # No credentials at all -> no key.
    ws = _make_websocket(app)
    assert auth.get_current_principal_websocket(websocket=ws, scopes=["read:monitor"]) is None
    assert captured["api_key"] is None
