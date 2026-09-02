"""Process-local TTL cache for bearer-token -> owner (google_sub)
resolution, in front of auth/store.py's backend lookups.

MultiTokenAuthMiddleware resolves the caller's token on every request to
/mcp, /api/, /otlp and /setup. On the Firestore backend each resolution
is several billed document reads on the hot path; the common case is the
same client polling repeatedly, so caching the resolved (owner, is_valid)
for a short TTL collapses that to ~one backend resolution per TTL.

Trade-off: a revoked token keeps working for up to TTL_SECONDS after
revocation (revoke routes call invalidate() to cut that to zero where
they hold the raw token).

In-process only: each Cloud Run instance has its own cache. maxScale is
small and per-instance convergence within a minute is fine.
"""

import threading
import time

# Long enough that a polling client pays ~one backend resolution per
# minute; short enough that a revoked/new token converges on its own.
TTL_SECONDS = 60

# Cap so a burst of distinct bad tokens (e.g. a scanner spraying headers)
# can't grow this unbounded. Well above any real concurrent-client count.
_MAX_ENTRIES = 2048

_lock = threading.Lock()
# token -> (expires_at, owner, is_valid). is_valid=False is cached too, so
# a repeated bad token doesn't re-hit the backend.
_entries: dict[str, tuple[float, str | None, bool]] = {}


def _purge_expired(now):
    stale = [k for k, (exp, _, _) in _entries.items() if exp <= now]
    for k in stale:
        del _entries[k]


def resolve(token, loader, on_miss=None):
    """Return (owner, is_valid) for `token`, using the cache when fresh.

    `loader` is called (outside the lock) only on a miss; it must return
    the owner google_sub for a valid token or None for an invalid one
    (auth_store.get_sub_for_token's contract). A non-None return is
    is_valid=True.

    `on_miss`, if given, is called with (token) after a successful
    resolution of a VALID token, on a cache miss only -- i.e. at most
    once per TTL_SECONDS per token. It must not raise.
    """
    now = time.time()
    with _lock:
        hit = _entries.get(token)
        if hit is not None and hit[0] > now:
            return hit[1], hit[2]

    owner = loader(token)
    is_valid = owner is not None

    with _lock:
        _purge_expired(now)
        if len(_entries) < _MAX_ENTRIES:
            _entries[token] = (now + TTL_SECONDS, owner, is_valid)
        # else: full of live entries; skip caching this one rather than
        # evict a possibly-hotter one. Correctness is unaffected.

    if is_valid and on_miss is not None:
        on_miss(token)
    return owner, is_valid


def invalidate(token):
    """Drop a token's cached resolution now, so a revoked credential stops
    working immediately rather than after the TTL. No-op if not cached."""
    if not token:
        return
    with _lock:
        _entries.pop(token, None)


def clear():
    """Wipe the cache (used between tests, and by device-revoke)."""
    with _lock:
        _entries.clear()
