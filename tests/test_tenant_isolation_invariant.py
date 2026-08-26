"""Enforces the tenant-isolation invariant as code, not just a docstring
promise: every route under /api/, /otlp (excluding pure ingestion
receivers, which attribute via the owner param already threaded through
handle_*_payload) or /setup that touches session/metrics/user data must
read `current_owner` somewhere in its own module-level source — either to
filter its response to the caller's own data (`current_owner.get()` passed
as an `owner=` filter) or to gate an admin-only route to the owner token
specifically (`current_owner.get() is None`, see oauth.py's
`_require_owner`). `current_owner` being `None` is the intentional
owner-token/all-data path (see app.py's contextvar docstring), not a bug —
this test isn't asserting every route rejects None, only that every route
actually *reads* current_owner rather than silently ignoring it, which is
exactly the gap a fresh review caught in GET /otlp/debug (see
mcp_server/otlp/__init__.py's owner-keyed _counts).

This is a source-inspection test, not a runtime one — it walks the
Starlette route table registered by importing mcp_server.app, resolves
each matching endpoint back to its Python source, and checks the source
text for `current_owner`. This catches "someone added a new route and
forgot to scope it" at test time, which a hand-written docstring rule
would not."""

import inspect

import pytest

from mcp_server import app as app_module  # noqa: F401 — import registers all routes
from mcp_server.server import server

# Prefixes that carry session/metrics/user data and therefore must respect
# tenant isolation. Deliberately excludes /oauth/* (client registration/
# token issuance — its own record-level ownership, not per-caller data
# filtering) and /auth/* (pre-auth sign-in, unauthenticated by design).
_SCOPED_PREFIXES = ("/api/", "/otlp", "/setup")

# Pure ingestion receivers already thread `owner=current_owner.get()`
# through at the call site in otlp.py (see routes/otlp.py's otlp_logs/
# otlp_metrics/otlp_traces) and pass it down into handle_*_payload, which
# is what actually attributes stored data — checking the receiver route's
# own source text would still pass (it does read current_owner.get()), so
# these aren't special-cased out; listed here only for documentation.
#
# The two oauth-admin routes below gate through a shared helper,
# _require_owner (routes/oauth.py), rather than reading current_owner
# inline in the endpoint body — legitimately correct (DRY across four
# routes), but invisible to a check of the endpoint's own source alone.
_ADMIN_GATED_VIA_HELPER = {
    ("GET", "/api/oauth-clients"),
    ("DELETE", "/api/oauth-clients/{client_id}"),
    ("GET", "/api/oauth-tokens"),
    ("DELETE", "/api/oauth-tokens/{token_prefix}"),
}

# /setup/* routes don't filter or attribute *stored* data by owner at
# all — they read the caller's own bearer token straight from the
# Authorization header and embed it into a config patch/script for that
# same caller to run locally. There's no cross-tenant data to leak: the
# token in the response is the exact token the caller authenticated
# with. current_owner (the resolved google_sub) is simply the wrong
# concept for this route's job, so it's excluded here rather than forced
# to read a value it has no use for.
_NOT_OWNER_SCOPED_BY_DESIGN = {
    ("POST", "/setup/apply-local-config"),
    ("GET", "/setup/local-script"),
    # Same reasoning: /setup/issue-install-code reads the caller's own
    # bearer token from the Authorization header and mints a code bound
    # to that exact token (see auth_store.issue_install_code) — no
    # current_owner-filtered data involved. /setup/install exchanges that
    # code back for the same token via auth_store.redeem_install_code —
    # the code itself, not current_owner, is the credential for this one
    # exchange (a piped curl command can't carry a bearer header).
    ("POST", "/setup/issue-install-code"),
    ("GET", "/setup/install"),
}

_KNOWN_SCOPED_ROUTES = {
    ("POST", "/otlp/v1/logs"),
    ("POST", "/otlp/v1/metrics"),
    ("POST", "/otlp/v1/traces"),
    ("GET", "/otlp/debug"),
    ("GET", "/api/sessions"),
    ("GET", "/api/sessions/{session_id}"),
    ("GET", "/api/tool-metrics"),
    ("GET", "/api/cost"),
    ("GET", "/api/context-timeline/{session_id}"),
    ("POST", "/api/record-session"),
    ("GET", "/api/oauth-clients"),
    ("DELETE", "/api/oauth-clients/{client_id}"),
    ("GET", "/api/oauth-tokens"),
    ("DELETE", "/api/oauth-tokens/{token_prefix}"),
    ("POST", "/setup/apply-local-config"),
    ("GET", "/setup/local-script"),
    ("POST", "/setup/issue-install-code"),
    ("GET", "/setup/install"),
}


def _scoped_routes():
    http_app = server.streamable_http_app()
    found = []
    for route in http_app.routes:
        path = getattr(route, "path", None)
        if path is None or not path.startswith(_SCOPED_PREFIXES):
            continue
        methods = getattr(route, "methods", None) or set()
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        for method in methods:
            if method == "HEAD":
                continue
            found.append((method, path, endpoint))
    return found


def test_known_scoped_routes_matches_registered_routes():
    """A canary for this test file itself going stale: if a new route is
    added under /api/, /otlp, or /setup without anyone updating
    _KNOWN_SCOPED_ROUTES, this fails loudly instead of the new route
    silently skating through unscoped."""
    registered = {(method, path) for method, path, _ in _scoped_routes()}
    missing = registered - _KNOWN_SCOPED_ROUTES
    assert not missing, (
        f"New route(s) under {_SCOPED_PREFIXES} not covered by this test: {missing}. "
        "Add them to _KNOWN_SCOPED_ROUTES once you've confirmed they read current_owner."
    )
    stale = _KNOWN_SCOPED_ROUTES - registered
    assert not stale, f"_KNOWN_SCOPED_ROUTES lists route(s) no longer registered: {stale}"


_ROUTES_NEEDING_OWNER_CHECK = [
    (method, path, endpoint)
    for method, path, endpoint in _scoped_routes()
    if (method, path) not in _NOT_OWNER_SCOPED_BY_DESIGN
]


@pytest.mark.parametrize(
    "method,path,endpoint",
    _ROUTES_NEEDING_OWNER_CHECK,
    ids=lambda v: f"{v[0]} {v[1]}" if isinstance(v, str) else None,
)
def test_scoped_route_reads_current_owner(method, path, endpoint):
    """Every session/metrics/user-data route must read `current_owner`
    somewhere in its own function body, or delegate to a helper in the
    same module that does (see _ADMIN_GATED_VIA_HELPER) — either to
    filter its response (`owner=current_owner.get()`) or to gate an
    owner-only admin action (`current_owner.get() is None`).
    `current_owner` being unread is exactly the bug this test exists to
    catch: GET /otlp/debug used to return process-global counters with
    no reference to current_owner at all — see
    mcp_server/otlp/__init__.py's owner-keyed _counts."""
    source = inspect.getsource(endpoint)
    if "current_owner" in source:
        return
    if (method, path) in _ADMIN_GATED_VIA_HELPER:
        module_source = inspect.getsource(inspect.getmodule(endpoint))
        assert "current_owner" in module_source, (
            f"{method} {path} is listed as gated via a module-level helper, but "
            f"{endpoint.__module__} has no current_owner reference at all."
        )
        return
    pytest.fail(
        f"{method} {path} ({endpoint.__module__}.{endpoint.__name__}) never reads "
        "current_owner — every route touching session/metrics/user data must filter "
        "by it (or explicitly gate on current_owner.get() is None for an owner-only "
        "admin route). See this file's module docstring."
    )
