"""Unit tests for mcp_server.auth.token_cache. The token->owner lookup is
a stub loader here; only the caching/invalidation contract is tested."""

import time

import pytest

from mcp_server.auth import token_cache


@pytest.fixture(autouse=True)
def _clean_cache():
    token_cache.clear()
    yield
    token_cache.clear()


def test_miss_then_hit_calls_loader_once():
    calls = []

    def loader(tok):
        calls.append(tok)
        return "sub-123"

    assert token_cache.resolve("tok-a", loader) == ("sub-123", True)
    assert token_cache.resolve("tok-a", loader) == ("sub-123", True)
    assert calls == ["tok-a"]  # second call served from cache


def test_invalid_token_is_cached_too():
    calls = []

    def loader(tok):
        calls.append(tok)
        return None  # get_sub_for_token's "unknown/invalid" return

    assert token_cache.resolve("bad", loader) == (None, False)
    assert token_cache.resolve("bad", loader) == (None, False)
    assert calls == ["bad"]  # a repeated bad token doesn't re-hit the backend


def test_ttl_expiry_re_resolves(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(time, "time", lambda: t[0])
    calls = []

    def loader(tok):
        calls.append(t[0])
        return "sub-x"

    token_cache.resolve("tok", loader)
    t[0] += token_cache.TTL_SECONDS - 1
    token_cache.resolve("tok", loader)  # still fresh
    t[0] += 2
    token_cache.resolve("tok", loader)  # now expired
    assert len(calls) == 2


def test_invalidate_forces_next_resolve_to_reload():
    calls = []

    def loader(tok):
        calls.append(tok)
        return "sub-1"

    token_cache.resolve("tok", loader)
    token_cache.invalidate("tok")
    token_cache.resolve("tok", loader)
    assert calls == ["tok", "tok"]


def test_invalidate_unknown_token_is_noop():
    token_cache.invalidate("never-seen")  # must not raise
    token_cache.invalidate("")
    token_cache.invalidate(None)


def test_clear_wipes_everything():
    token_cache.resolve("a", lambda _t: "s")
    token_cache.resolve("b", lambda _t: "s")
    token_cache.clear()
    calls = []
    token_cache.resolve("a", lambda _t: (calls.append(1), "s")[1])
    assert calls == [1]


def test_on_miss_runs_only_on_miss_not_on_hit():
    misses = []

    def loader(_t):
        return "sub-9"

    def on_miss(tok):
        misses.append(tok)

    token_cache.resolve("tok", loader, on_miss=on_miss)
    token_cache.resolve("tok", loader, on_miss=on_miss)  # cache hit
    token_cache.resolve("tok", loader, on_miss=on_miss)  # cache hit
    assert misses == ["tok"]


def test_on_miss_not_called_for_invalid_token():
    misses = []
    token_cache.resolve("bad", lambda _t: None, on_miss=misses.append)
    assert misses == []


def test_cache_does_not_grow_unbounded(monkeypatch):
    monkeypatch.setattr(token_cache, "_MAX_ENTRIES", 5)
    for i in range(20):
        token_cache.resolve(f"tok-{i}", lambda _t: "s")
    # Never exceeds the cap; entries past it just aren't cached.
    assert len(token_cache._entries) <= 5


def test_expired_entries_are_purged_on_next_resolve(monkeypatch):
    t = [0.0]
    monkeypatch.setattr(time, "time", lambda: t[0])
    for i in range(10):
        token_cache.resolve(f"tok-{i}", lambda _t: "s")
    assert len(token_cache._entries) == 10
    t[0] += token_cache.TTL_SECONDS + 1
    token_cache.resolve("fresh", lambda _t: "s")
    # The 10 stale entries were purged; only "fresh" remains.
    assert len(token_cache._entries) == 1
