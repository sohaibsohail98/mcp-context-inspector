# Next steps

Written for a fresh context window. See `docs/output.md` for full context on
what's already shipped and live-verified — this is only the remainder,
in rough priority order.

## 1. Click-test the downloadable setup script in a real browser (highest priority — smallest gap left)

Everything except the actual click has been verified (download → subprocess
execution → correct `settings.json`, see `docs/output.md` §4). What's
missing: open the deployed connect page in a real browser, click "Download
setup script," confirm it lands in Downloads, run
`python3 mcp-context-inspector-setup.py`, restart Claude Code, confirm a
session appears on the dashboard. Closes the one remaining "tested by
subprocess, not by a human clicking a button" gap.

## 2. Ephemeral storage on Cold Run cold start (real risk, not new, not fixed)

`data/mcp_auth.db` (sign-in tokens, and now OAuth client registrations +
tokens) and `data/metrics.db` both live on Cloud Run's ephemeral container
filesystem. `min-instances=0` means a cold start wipes them. Concretely:
if the container recycles, the claude.ai Connector you just connected will
start showing "Connection issue" until you disconnect and re-add it — the
registered `client_id` is gone.

Two ways to actually fix this, not just document around it:
- **Cheapest**: set `min-instances=1` on the Cloud Run service. Keeps one
  instance warm always, trading a small always-on cost for the container
  never cold-starting (and never losing this data) under normal traffic.
  One `gcloud run services update` command.
- **Correct fix**: swap SQLite for a real persistent backend. The OTLP
  work already added a DynamoDB backend for metrics
  (`metrics/store_dynamodb.py`) — the equivalent doesn't exist yet for
  `auth_store.py`. Given `auth_store.py`'s own docstring has flagged this
  as needed "before this server runs somewhere with an ephemeral
  filesystem" since before this session, and it now also holds OAuth
  client/token data, this is worth actually doing rather than deferring
  again.

## 3. Live-test Cursor and Copilot against the OAuth implementation

The OAuth server is generic, spec-compliant (RFC 9728/8414/7591 + PKCE) —
nothing claude.ai-specific in the code. It should work for any MCP client
that does proper discovery. That's currently an inference from spec
compliance, not a live test the way claude.ai now is. Worth actually
trying both, the same way claude.ai just got tested: add this server as a
connector/MCP source in Cursor and in Copilot, confirm the OAuth flow
completes and tools actually respond.

## 4. Admin visibility into OAuth clients/tokens

`auth_store.py` has `list_users()`/`revoke()` for the plain sign-in
tokens, but nothing equivalent for OAuth clients or OAuth-issued tokens —
no way to see which clients have registered, or to revoke one client's
access without deleting the whole `mcp_auth.db`. Low priority for a
personal-scale server, but worth a `list_oauth_clients()` /
`revoke_oauth_client(client_id)` pair if this ever needs to support
untrusted or semi-trusted clients.

## 5. Dashboard mockup-vs-shipped gaps (unrelated to this session, still open)

From `docs/MOCKUP_VS_SHIPPED_GAPS.md`, not touched this session: KPI strip
stubs (cache-hit-rate/tool-error-rate/context-alerts), session-list
metric swap (tokens·cost shown instead of turn count + context-alert
dot), missing source-filter chips, and insight cards (a minimal 2-3 card
version was a stated v1 requirement, never built). Independent of
everything in `docs/output.md` — a separate piece of work whenever it's
picked back up.

## 6. Short-lived OAuth token handshake (deliberately deferred, revisit only if needed)

Current OAuth tokens don't expire (matching this server's existing
bearer-token model). If this project's threat model changes — e.g.
shared-machine use becomes common — revisit adding real token expiry +
refresh-token rotation. Not needed today; noted so it isn't re-litigated
from scratch later.
