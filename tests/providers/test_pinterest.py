"""Tests for PinterestProvider OAuth scopes."""

from urllib.parse import parse_qs, urlsplit

from providers.pinterest import PinterestProvider

# The scopes Pinterest's v5 spec lists for ``POST /pins`` (operationId
# pins/create). Missing any one of them fails every publish with a 401.
CREATE_PIN_SCOPES = {"boards:read", "boards:write", "pins:read", "pins:write"}


def _provider():
    return PinterestProvider({"client_id": "id", "client_secret": "secret"})


def test_auth_url_requests_every_scope_creating_a_pin_needs():
    url = _provider().get_auth_url("https://app.example/cb", "state-123")

    requested = set(parse_qs(urlsplit(url).query)["scope"][0].split(","))
    assert requested >= CREATE_PIN_SCOPES


def test_auth_url_still_requests_profile_access():
    url = _provider().get_auth_url("https://app.example/cb", "state-123")

    requested = set(parse_qs(urlsplit(url).query)["scope"][0].split(","))
    assert "user_accounts:read" in requested
