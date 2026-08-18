"""Regression tests for mcp_server/google_auth.py — no real network calls
to Google; `id_token.verify_oauth2_token` is monkeypatched.
"""

import pytest

from mcp_server import google_auth


def test_verify_credential_returns_sub_and_email(monkeypatch):
    monkeypatch.setattr(
        google_auth.id_token,
        "verify_oauth2_token",
        lambda credential, request, client_id: {"sub": "12345", "email": "a@example.com", "aud": client_id},
    )
    identity = google_auth.verify_credential("fake-jwt", "my-client-id")
    assert identity == {"sub": "12345", "email": "a@example.com"}


def test_verify_credential_missing_email_does_not_crash(monkeypatch):
    """Google ID tokens always carry `sub`, but `email` is only present
    if the `email` scope was granted — must not KeyError."""
    monkeypatch.setattr(
        google_auth.id_token,
        "verify_oauth2_token",
        lambda credential, request, client_id: {"sub": "12345"},
    )
    identity = google_auth.verify_credential("fake-jwt", "my-client-id")
    assert identity == {"sub": "12345", "email": None}


def test_verify_credential_raises_invalid_google_token_on_bad_jwt(monkeypatch):
    def _raise(credential, request, client_id):
        raise ValueError("Token expired")

    monkeypatch.setattr(google_auth.id_token, "verify_oauth2_token", _raise)
    with pytest.raises(google_auth.InvalidGoogleToken):
        google_auth.verify_credential("expired-jwt", "my-client-id")
