"""Basic reachability + auth-gating smoke tests."""


def test_health_is_reachable_unauthenticated(client):
    resp = client.health()
    assert resp.status == 200
    assert resp.body == {"status": "ok"}


def test_sessions_requires_auth(client):
    resp = client._request("GET", "/api/sessions", auth=False)
    assert resp.status == 401


def test_sessions_rejects_bad_token(client):
    resp = client._request("GET", "/api/sessions", headers={"Authorization": "Bearer not-a-real-token"}, auth=False)
    assert resp.status == 401


def test_sessions_accepts_configured_token(client):
    resp = client.sessions()
    assert resp.status == 200
    assert isinstance(resp.body, list)
