"""Regression tests for mcp_server/google_auth.py, with no real network calls
to Google; `id_token.verify_oauth2_token` is monkeypatched.
"""

import pytest

from mcp_server.auth import google as google_auth


def test_verify_credential_returns_sub_and_email(monkeypatch):
    monkeypatch.setattr(
        google_auth.id_token,
        "verify_oauth2_token",
        lambda credential, request, client_id: {
            "sub": "12345", "email": "a@example.com", "email_verified": True, "aud": client_id,
        },
    )
    identity = google_auth.verify_credential("fake-jwt", "my-client-id")
    assert identity == {"sub": "12345", "email": "a@example.com"}


def test_verify_credential_rejects_unverified_email(monkeypatch):
    """An email claim google itself marks unverified shouldn't be trusted
    as someone's real identity, since it's used for display/attribution
    throughout (dashboard identity, OAuth-issued token records)."""
    monkeypatch.setattr(
        google_auth.id_token,
        "verify_oauth2_token",
        lambda credential, request, client_id: {
            "sub": "12345", "email": "a@example.com", "email_verified": False,
        },
    )
    with pytest.raises(google_auth.InvalidGoogleToken):
        google_auth.verify_credential("fake-jwt", "my-client-id")


def test_verify_credential_missing_email_does_not_crash(monkeypatch):
    """Google ID tokens always carry `sub`, but `email` is only present
    if the `email` scope was granted, and must not KeyError."""
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


def test_verify_credential_passes_our_client_id_as_the_expected_audience(monkeypatch):
    """If this server's own GOOGLE_OAUTH_CLIENT_ID weren't correctly
    threaded through as the audience check, a credential minted for a
    DIFFERENT app entirely could be accepted here. verify_oauth2_token
    only enforces the audience match if it's actually passed the real
    client_id, not a stale/wrong one."""
    seen = {}

    def _fake_verify(credential, request, client_id):
        seen["client_id"] = client_id
        if client_id != "the-real-client-id":
            raise ValueError("Wrong audience")
        return {"sub": "12345", "email": "a@example.com", "email_verified": True}

    monkeypatch.setattr(google_auth.id_token, "verify_oauth2_token", _fake_verify)

    google_auth.verify_credential("jwt-for-a-different-app", "the-real-client-id")
    assert seen["client_id"] == "the-real-client-id"

    with pytest.raises(google_auth.InvalidGoogleToken):
        google_auth.verify_credential("jwt-for-a-different-app", "some-other-client-id")
