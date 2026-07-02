"""Side-C compatibility: ``bluesky-queueserver-api`` token/session auth over HTTP.

Companion to ``test_side_c_api_client_compat.py`` (which drives the client in
single-user API-key mode). Here the server is configured with a real
identity-provider authenticator (``DictionaryAuthenticator``) so the client's
token/session flow -- ``login()`` -> token-authenticated requests ->
``session_refresh()`` -> ``apikey_new()`` -> ``logout()`` -- is exercised through
the actual PyPI client, guarding the auth surface the HTTP transport depends on.
"""

import pytest
from tests.manager.common import re_manager, re_manager_factory  # noqa: F401

from tests.http.conftest import (  # noqa: F401
    SERVER_ADDRESS,
    SERVER_PORT,
    fastapi_server_fs,
    setup_server_with_config_file,
)

from bluesky_queueserver_api.http import REManagerAPI

_HTTP_URI = f"http://{SERVER_ADDRESS}:{SERVER_PORT}"

# A server with a password authenticator ("toy" provider) plus anonymous reads.
# The client reaches the provider's token endpoint at /api/auth/provider/toy/token.
CONFIG_TOY_AUTH = """
authentication:
    allow_anonymous_access: True
    providers:
        - provider: toy
          authenticator: queueserver_service.http.authenticators:DictionaryAuthenticator
          args:
              users_to_passwords:
                  bob: bob_password
                  alice: alice_password
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
"""


def test_side_c_token_session_auth(
    tmpdir,
    monkeypatch,
    re_manager,  # noqa: F811
    fastapi_server_fs,  # noqa: F811
):
    setup_server_with_config_file(config_file_str=CONFIG_TOY_AUTH, tmpdir=tmpdir, monkeypatch=monkeypatch)
    fastapi_server_fs()

    # Anonymous access is permitted for reads by this config.
    anon = REManagerAPI(http_server_uri=_HTTP_URI)
    try:
        assert "manager_state" in anon.status(), "anonymous read should be allowed"
    finally:
        anon.close()

    # The client posts credentials to the provider's token endpoint; the provider
    # string carries the full '/toy/token' path segment.
    rm = REManagerAPI(http_server_uri=_HTTP_URI, http_auth_provider="/toy/token")
    try:
        login = rm.login(username="bob", password="bob_password")
        assert "access_token" in login and "refresh_token" in login, login
        assert rm.auth_key, "client should hold an auth key after login"

        # Token-authenticated requests work (bob has the admin role).
        assert rm.status()["manager_state"] == "idle"
        assert rm.permissions_get()["success"], "token-authenticated permissions_get failed"

        # Rotate the session; the client stores the refresh token from login.
        refreshed = rm.session_refresh()
        assert "access_token" in refreshed, refreshed
        assert rm.status()["manager_state"] == "idle", "status failed after session refresh"

        # An API key can be minted from the authenticated session.
        new_key = rm.apikey_new(expires_in=900)
        assert "secret" in new_key, new_key

        rm.logout()
    finally:
        rm.close()

    # Wrong credentials are rejected with an HTTP client error.
    bad = REManagerAPI(http_server_uri=_HTTP_URI, http_auth_provider="/toy/token")
    try:
        with pytest.raises(bad.HTTPClientError):
            bad.login(username="bob", password="wrong_password")
    finally:
        bad.close()
