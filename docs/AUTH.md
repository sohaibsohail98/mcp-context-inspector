# Auth

Sign in, and you're scoped to your own data on this server — no
password, one click:

<img src="screenshots/signin-page.png" width="360" alt="Sign-in page">

You land on a ready-to-use config: your token, the MCP server URL, and
a paste-ready Claude Code / MCP-client config block.

<img src="screenshots/connected-page.png" width="360" alt="Connected — your token and config">

Two ways in, both accepted by the same `Authorization: Bearer <token>`
header on `/mcp` and every `/api/*` route:

1. **Owner token** — the `MCP_AUTH_TOKEN` env var. Yours, printed on
   startup. Fine for solo local use.
2. **Google sign-in, per person** — for anyone else you want to connect
   their own Bedrock-based agent to your server, without handing them your one
   token (and without being able to revoke just their access later).
   Set `GOOGLE_OAUTH_CLIENT_ID` (see setup below) and point them at
   `http://<your-host>:8787/auth/login` — they sign in with their own
   Google account, get a personal token minted for them
   (`mcp_server/auth_store.py`), and use that as their bearer token.
   Signing in again returns the *same* token, so pasting it into an MCP
   client config once doesn't get invalidated by a second sign-in.

This is bearer token plus a Google ID token, not the MCP spec's OAuth
2.1 authorization-server flow — a client user pastes a token rather
than clicking "sign in" inside their MCP client.

## Why not a full OAuth 2.1 authorization server

The "real" way an MCP client is meant to discover and authenticate, per
the MCP spec's OAuth Resource Server support, needs a genuine
authorization server — PKCE, dynamic client registration, a consent
screen, its own client/token tables — real infrastructure disproportionate
to a personal-scale server. This gets the property that actually matters
(each friend authenticates as themselves, with their own Google account,
and you can revoke just one person) via Google Identity Services'
one-tap credential flow instead: no redirect URIs, no client secret,
just a signed ID token verified server-side (`mcp_server/google_auth.py`).

## Google Cloud setup (one-time, ~2 minutes)

1. [console.cloud.google.com](https://console.cloud.google.com) → create
   or pick a project → **APIs & Services → Credentials**.
2. **Create Credentials → OAuth client ID** → Application type **Web
   application**.
3. Under **Authorized JavaScript origins**, add the origin(s) you'll
   serve `/auth/login` from — e.g. `http://localhost:8787` for local
   use, plus your real domain once deployed. Use `localhost`, not
   `127.0.0.1`: Google Identity Services only reliably honors
   `localhost` for the plain-HTTP local-dev exemption — the Console UI
   will let you save `127.0.0.1` as an origin, but the live sign-in
   widget can still reject it with "origin not allowed" at runtime. No
   redirect URI needed for this flow.
4. Copy the **Client ID** (safe to expose client-side — it's not a
   secret) and set it as `GOOGLE_OAUTH_CLIENT_ID` in your environment
   before starting the server.

## Data isolation

Every session is attributed to whoever recorded it — the owner token
sees everyone's (it's your server), a per-user token only ever sees,
queries, and records its own. There's no way to read or list another
person's session_ids, even by guessing one (a session that exists but
isn't yours reads back exactly like one that doesn't exist at all — see
`metrics/store_sqlite.py`'s `get_session_metrics` docstring). Revoke a
friend's access with `mcp_server.auth_store.revoke(google_sub)` (find
their `sub` via `list_users()`) if you need to cut someone off — their
existing token stops working immediately, and any data they already
recorded stays where it is (still owned by them, invisible to other
per-user tokens, visible to yours).

## Letting a friend's own agent record its own data

`record_session` isn't just a local Python import — it's also an
authenticated MCP tool (and `/api/record-session` REST route), so a
friend's agent running *anywhere*, not sharing your Python environment
or filesystem, can push its own sessions into your server, attributed to
them:

```python
# from the friend's own agent code, after it gets its own token from
# GET http://<your-host>:8787/auth/login
import httpx

httpx.post(
    "http://<your-host>:8787/api/record-session",
    headers={"Authorization": f"Bearer {their_token}"},
    json={"prompt": prompt, "model_id": model_id, "loop_result": loop_result},
)
```

Or the equivalent as a real MCP tool call (`record_session`) over their
own MCP client connection — same shape, same auth, same attribution.
Either way, only they (and you, the owner) can read it back afterward.
