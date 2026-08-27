# api_tests/

Live, black-box HTTP tests against a **real deployed** mcp-context-inspector
instance — not the in-process `starlette.testclient.TestClient` suite in
`tests/`, which never leaves the current process and can't catch a bug in
how the actual deployment (Cloud Run + the Cloudflare Worker proxy in
`cloudflare-proxy/`) is wired up, routed, or authenticated.

`tests/` answers "is the server's own logic correct." `api_tests/` answers
"is the thing actually running at the public URL correct" — auth, routing
through the Worker proxy, and whether data POSTed to `/otlp/*` is really
retrievable via `/api/*` afterward, exactly the gap that let telemetry
silently never reach the dashboard.

## Running locally

Needs two env vars — this suite refuses to guess a target, since these are
live tests that write real data:

```bash
export API_TEST_BASE_URL="https://ctxwindow.uk"
export API_TEST_TOKEN="<a valid owner or per-user bearer token>"
uv run python -m pytest api_tests/ -v
```

Without both vars set, every test in this suite is **skipped** (not failed —
see `conftest.py`), so it's safe to run `pytest` at the repo root without
these vars and get the normal `tests/` results only.

## What's covered

- `test_health.py` — reachability, and that `/api/sessions` actually
  enforces the bearer token (missing / wrong token both 401).
- `test_otlp.py` — the core regression coverage: POSTs a payload shaped like
  a real Claude Code OTLP export (`service.name: claude-code` + `session.id`
  on the resource attributes) to `/otlp/v1/logs`, asserts it's accepted
  (not silently counted as `"skipped"`), and polls `/api/sessions` until the
  resulting session is visible. Also locks in the documented behavior for an
  unrecognized-vendor payload (200 + `skipped`, not a 4xx) and for malformed
  JSON (400).
- `test_auth.py` — `/auth/login` reachability, and that every protected
  prefix (`/api/`, `/otlp`, `/mcp`) rejects a missing/invalid token with a
  401 carrying `WWW-Authenticate` (needed for MCP client OAuth discovery —
  see `mcp_server/middleware.py`).

Not covered: `/auth/verify`'s real Google-credential exchange — that needs
a live browser-driven OAuth round-trip, not something a black-box HTTP
script can drive. See `tests/test_oauth.py` for the monkeypatched,
in-process version of that flow.

## This writes real sessions to the real account behind API_TEST_TOKEN

`test_otlp.py` POSTs real OTLP payloads that land as real rows in whatever
account `API_TEST_TOKEN` belongs to — every session_id it creates is
prefixed `api-tests-`. `GET /api/sessions` hides anything with that prefix
by default for everyone (see `mcp_server/dev_mode.py`), so these probe
sessions don't clutter the normal dashboard view. An account listed in the
`DEV_MODE_SUBS` env var (or the shared owner token) can still see them via
the dashboard's "Test sessions: hidden/shown" toggle, or by passing
`?include_test_sessions=1` to `/api/sessions` directly.

## CI

Wired into `.github/workflows/tests.yml` as a separate `api-e2e-tests` job,
guarded to only run when `API_TEST_BASE_URL`/`API_TEST_TOKEN` repo secrets
are configured — see that file's comments. It does not block or share a
job with the existing unit-test job: these hit a real network endpoint and
must never be able to flake or gate merges the way a live-service test can.
