# User journey — full state, as of this session

Written for a fresh context window. Supersedes the "Connectors" claims in
`docs/USER_JOURNEY.md` §4 and the plan in `docs/DEPLOYED_ONBOARDING_PLAN.md` —
both are now implemented and live-verified, not just planned. This is the
authoritative "what works, what doesn't, what's proven" as of the deploy at
commit `4ee609d` (revision `mcp-context-inspector-00024-k8m`).

## 1. The journey, end to end

| # | Step | Status |
|---|------|--------|
| 1 | Discover (live demo or self-host) | Works. |
| 2 | Land on `/auth/login` | Works. |
| 3 | Sign in with Google | Works. |
| 4 | Land on config — primary action + collapsed "Advanced" | Works, redesigned this session (see §2). |
| 5a | Self-hosted: "Apply to my Claude Code config" | Works (verified previous session, live end-to-end). |
| 5b | Deployed: "Download setup script" | Script downloads, is valid Python, and a dedicated test runs it as a real subprocess and asserts the resulting `settings.json` is correct. **Not yet verified via an actual browser click** — the one remaining gap in this path (see nextsteps.md). |
| 5c | Deployed: claude.ai Connectors | **Works, live-verified this session** — see §3. This was broken earlier today (registration silently failed) and is now fixed and confirmed. |
| 6 | Dashboard | Works. |
| 7 | Use it (sessions appear within ~5–8s) | Works for local self-hosters (§5a) and now, in principle, for anyone using Connectors + `record_session` (§3) — telemetry auto-populate still needs the manual OTLP snippet, see §4. |

## 2. Connect page redesign (this session)

- Primary action adapts to context: "Apply to my Claude Code config" (self-hosted) or "Download setup script" (deployed), both prominent.
- Secondary link next to it: "Connect via claude.ai Connectors instead" — now a real `href` (was `href="#"` with `return false`, i.e. a dead link — found and fixed this session).
- Everything manual (4-tab config card, Connectors walkthrough) collapsed by default behind an "Advanced" `<details>`.
- Connectors instructions rewritten: no longer say to paste a token as a header (never worked); now correctly say paste the URL and sign in with Google.
- Connectors link updated to the current `claude.ai/new#settings/customize-connectors` URL (the old `/settings/connectors` URL was stale, per live user report).

## 3. claude.ai Connectors — now real, live-verified

**What was broken this morning:** clicking "Add" on a custom connector pointed at this server produced *"Couldn't register with mcp-context-inspector's sign-in service."* Root cause: this server had no OAuth support at all — claude.ai's connector dialog only offers OAuth Client ID/Secret fields (no plain bearer-token field), and leaving them blank triggers claude.ai attempting OAuth Dynamic Client Registration, which failed outright against a server with no `/oauth/register` endpoint.

**What was built:** a real, minimal, spec-compliant OAuth 2.1 + PKCE authorization server (`mcp_server/server.py`'s `/oauth/*` + `/.well-known/*` routes, backed by new tables in `mcp_server/auth_store.py`):
- RFC 9728 protected-resource metadata, RFC 8414 authorization-server metadata, RFC 7591 dynamic client registration, PKCE (S256-only) authorization-code grant.
- Reuses this server's existing Google sign-in as the consent step — no separate login system.
- Each OAuth client gets its own freshly-minted token, distinct from the plain sign-in token, so disconnecting a Connector later can't break a paste-in-config client.
- 22 dedicated tests (`tests/test_oauth.py`) covering the full round trip and every failure mode (PKCE mismatch, replay, redirect_uri mismatch, expiry, unknown client, resource mismatch).

**Two more bugs found only by testing against the real deployed server** (neither was catchable by in-process tests, since neither condition exists there):

1. **Wrong public origin.** Every OAuth URL this server generated about itself was built from `request.base_url`, which — behind the Cloudflare Worker reverse proxy — reflects Cloud Run's internal `http://*.run.app` origin, not the public `https://mcp-inspector.sohaibsohail.workers.dev` URL a client actually used. claude.ai's discovery fetch followed that wrong, insecure URL and registration failed. Fixed with a `PUBLIC_ORIGIN` env var (same pattern as the existing `CHAT_UI_ORIGIN`/`MCP_ALLOWED_HOSTS`), now set on the live Cloud Run service.
2. **CORS preflight blocked at the app-wide middleware level.** `CORSMiddleware`'s `CHAT_UI_ORIGIN` allowlist rejected every `OPTIONS` preflight regardless of path — so claude.ai's own preflight to `/oauth/register` never reached that route's permissive per-route CORS handling. Fixed with `OAuthCORSMiddleware`, a new outermost middleware scoped only to `/oauth/*` and `/.well-known/oauth-*`; `/api/*` and friends keep their existing restriction (verified by a dedicated test that the fix didn't accidentally loosen anything else).

Both fixes are covered by `tests/test_oauth_cors_and_origin.py`, which — following the same lesson as the pre-existing 421 Host-header regression test — builds the app the way `__main__` actually does (full middleware stack), not the bare `streamable_http_app()` other test files use.

**Live test, this session (real, not inferred):** after redeploying with both fixes, direct `curl` against the live server confirmed correct public HTTPS URLs everywhere and a successful CORS preflight from `Origin: https://claude.ai`. You then disconnected and re-added the connector in claude.ai for real. Cloud Run's request logs show, on the deployed revision with both fixes:

```
14:20:15  GET   /.well-known/oauth-protected-resource/mcp   200
14:20:16  GET   /.well-known/oauth-authorization-server     200
14:20:16  POST  /oauth/register                             201
14:20:17  GET   /oauth/authorize?...redirect_uri=https://claude.ai/api/mcp/auth_callback...  200
14:20:22  POST  /oauth/authorize                             200
14:20:22  POST  /oauth/token                                 200
14:20:23  POST  /mcp                                         200  (444 bytes)
14:20:24  POST  /mcp                                         200  (4022 bytes)
14:20:26  POST  /mcp                                         200  (444 bytes)
14:20:26  POST  /mcp                                         200  (4022 bytes)
14:20:26  POST  /mcp                                         200  (295 bytes)
14:20:26  POST  /mcp                                         200  (297 bytes)
```

The two 4022-byte responses are almost certainly `tools/list` — this server exposes 8 tools, which matches exactly what your claude.ai Connectors screenshot shows ("Other tools 8": Get agent trace, Get context timeline, Get cost estimate, Get recent sessions, Get session metrics, Get token breakdown, Get tool metrics, Record session). This is a real, working, authenticated MCP session established by claude.ai's own backend against the live server — not a simulated test.

**Known limitation, inherited, not new:** the OAuth client registration and issued tokens live in the same ephemeral SQLite file (`data/mcp_auth.db`) as the existing sign-in tokens, which `docs/DEPLOYMENT.md` already documents as resetting on a Cloud Run cold start (`min-instances=0`). If the container scales to zero and a new one starts, the registered `client_id` is gone and claude.ai's connector will show "Connection issue" until you disconnect and re-add it. This is the same accepted tradeoff the sign-in system already has, not a new regression — but it's worth knowing before relying on the connector staying up unattended for a long idle period.

## 4. What still doesn't fully work / isn't proven

- **OTLP auto-telemetry still isn't available via Connectors.** This is inherent to how Connectors work (MCP connection only, no environment variables) — not something this fix could address. The dashboard auto-populating as you code still needs either §5a/§5b (local write) or the manual "Claude Code (live telemetry)" snippet.
- **The downloadable script (§5b) has never been run by an actual browser click.** Tested thoroughly as a subprocess (download → write to disk → execute → assert `settings.json` is correct), but not "click the button in a real browser, watch the file land in Downloads, run it" end to end.
- **Cursor and Copilot were never tested against this OAuth implementation.** It's generic, spec-compliant OAuth — nothing claude.ai-specific in the code — so it should work for any MCP client that does proper discovery, but that's an inference from spec compliance, not a live test the way claude.ai now is.
- **Ephemeral storage** (see §3's "known limitation") applies to the whole auth system, not just OAuth — a real production risk for a server with `min-instances=0` that this session didn't fix (out of scope, pre-existing, documented).
- **The mockup-vs-shipped dashboard gaps** from `docs/MOCKUP_VS_SHIPPED_GAPS.md` (KPI stubs, session-list metric swap, missing source filter, insight cards) are unrelated to this session's work and remain open.

## 5. Everything fixed/shipped this session, in order

1. Downloadable setup script for deployed instances (`/setup/local-script`, `mcp_server/local_setup.py`) + redesigned connect page.
2. Redacted a real bearer token that had been committed verbatim in `docs/USER_JOURNEY.md`; untracked an internal planning doc (`docs/internal/OTLP_INTEGRATION_PLAN.md`) that `.gitignore` should have caught but didn't (committed before the ignore rule existed).
3. Merged the whole `otlp-telemetry-integration` branch (OTLP ingestion, one-click local setup, the above) into `main` and deployed to prod.
4. Fixed the dead "Connect via claude.ai Connectors instead" link (`href="#"` → real link).
5. Built a real OAuth 2.1 + PKCE authorization server so Connectors actually works.
6. Fixed the wrong-public-origin bug (`PUBLIC_ORIGIN` env var + `_public_origin()` helper).
7. Fixed the CORS-preflight-blocked-at-the-middleware-level bug (`OAuthCORSMiddleware`).
8. Updated the claude.ai Connectors URL to the current one.
9. Bumped `actions/checkout`, `astral-sh/setup-uv`, `google-github-actions/auth`, `google-github-actions/setup-gcloud` off deprecated Node 20 runtimes (cleared the CI warnings).
10. Live-verified the entire OAuth flow against the real deployed server, with you completing a real connector add in claude.ai and Cloud Run's logs confirming a genuine authenticated MCP session.

All work is on `main`, deployed, and covered by 182 passing tests (`uv run pytest`).
