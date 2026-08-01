"""Unit tests for `app/admin.py`'s auth gate (`_check_auth`)."""

from __future__ import annotations

from app.admin import _check_auth


class _FakeAppState:
    def __init__(self, admin_api_token):
        self.admin_api_token = admin_api_token


class _FakeApp:
    def __init__(self, admin_api_token):
        self.state = _FakeAppState(admin_api_token)


class _FakeRequest:
    def __init__(self, admin_api_token, headers=None):
        self.app = _FakeApp(admin_api_token)
        self.headers = headers or {}


def test_check_auth_fails_closed_when_no_token_configured():
    request = _FakeRequest(admin_api_token=None, headers={"authorization": "Bearer anything"})
    response = _check_auth(request)
    assert response is not None
    assert response.status_code == 403


def test_check_auth_fails_closed_when_token_is_empty_string():
    request = _FakeRequest(admin_api_token="", headers={"authorization": "Bearer anything"})
    response = _check_auth(request)
    assert response is not None
    assert response.status_code == 403


def test_check_auth_rejects_missing_authorization_header():
    request = _FakeRequest(admin_api_token="secret")
    response = _check_auth(request)
    assert response is not None
    assert response.status_code == 401


def test_check_auth_rejects_wrong_scheme():
    request = _FakeRequest(admin_api_token="secret", headers={"authorization": "Basic secret"})
    response = _check_auth(request)
    assert response is not None
    assert response.status_code == 401


def test_check_auth_rejects_wrong_token():
    request = _FakeRequest(admin_api_token="secret", headers={"authorization": "Bearer wrong"})
    response = _check_auth(request)
    assert response is not None
    assert response.status_code == 401


def test_check_auth_accepts_matching_bearer_token():
    request = _FakeRequest(admin_api_token="secret", headers={"authorization": "Bearer secret"})
    response = _check_auth(request)
    assert response is None
