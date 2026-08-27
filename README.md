<p align="center">
  <img src="assets/ctxwindow-logo.svg" alt="CtxWindow" width="360">
</p>

<p align="center">
  A drop-in MCP server that gives Claude Code, Bedrock-based agents, and other
  MCP/LLM clients real per-session cost, token, and tool metrics, plus a full
  <strong>Context Window Explorer</strong>, over a real MCP handshake.
</p>

<p align="center">
  <a href="https://github.com/sohaibsohail98/mcp-context-inspector/actions/workflows/tests.yml"><img src="https://github.com/sohaibsohail98/mcp-context-inspector/actions/workflows/tests.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License: MIT"></a>
</p>

> **Independent, unaffiliated open-source project.** ctxwindow is not built, maintained, or
> endorsed by Anthropic. "Claude" and "Claude Code" are Anthropic's products; ctxwindow
> reads their publicly documented OpenTelemetry export and MCP protocol, nothing more.

The package/repo name on disk stays `mcp-context-inspector`; the product it ships is
called **ctxwindow** (after its domain, [ctxwindow.uk](https://ctxwindow.uk)).

**[Docs site →](https://ctxwindow.uk/docs)**: same content as this
README, laid out as a proper single-page reference with section nav.

## Why this exists

Most agent observability tools re-show data your own UI already displays. ctxwindow shows
something you normally can't see at all: system prompt, tool specs, reasoning, tool calls
and results, and the final answer, in the order they actually entered context. Each block
is measured against the model's real context window and marked as either visible to the
user or invisible overhead. Token counts are honest, labeled estimates, not exact provider
usage (see [Architecture](#architecture) for why that tradeoff is the right one here).

Anthropic's Claude Code docs page,
["Explore the context window"](https://code.claude.com/docs/en/context-window), is an
interactive simulation of what loads into a session and what each file read costs. It
motivated wanting the same visibility for an arbitrary agent loop, not just Claude Code.

<img src="screenshots/signin-page.png" width="380" alt="Sign-in page"> <img src="screenshots/connected-page.png" width="380" alt="Signed in: your own live dashboard">

## Try it in 30 seconds

Live demo: **https://ctxwindow.uk**

It's seeded with fixture data and runs on SQLite, so writes made behind Google sign-in
don't survive a cold start. That's the right tradeoff for a free public demo, not a
limitation of the storage layer (see [Storage backends](#storage-backends)).

Not yet published to PyPI, so run it from source:

```sh
git clone https://github.com/sohaibsohail98/mcp-context-inspector
cd mcp-context-inspector && uv run python -m mcp_server.server
```

With no `MCP_AUTH_TOKEN` set, the server generates and prints one on startup, using the
same trust model as a Jupyter server's printed token.

## Installation

Requires Python 3.11+.

```sh
git clone https://github.com/sohaibsohail98/mcp-context-inspector
cd mcp-context-inspector
uv sync
```

## Run it locally

```sh
cd mcp-context-inspector
uv sync

# Fixed owner token so it doesn't rotate on every restart, and a scratch
# SQLite file instead of data/metrics.db.
export MCP_AUTH_TOKEN=local-test-owner-token
export STORAGE_BACKEND=sqlite
export METRICS_DB_PATH=/tmp/mci-local-test.db
export MCP_SERVER_PORT=8787

# Optional: enables the real "Sign in with Google" button. Without it,
# /auth/login still renders and works with the owner token only. See
# "Google sign-in setup" below for how to get a client ID.
export GOOGLE_OAUTH_CLIENT_ID=<your-client-id>.apps.googleusercontent.com

uv run python -m mcp_server.server
```

Open `http://localhost:8787` (use `localhost`, not `127.0.0.1`, since Google sign-in
needs it) and use `local-test-owner-token` as the bearer token for the config it
generates.

To see the dashboard populate, point a real Claude Code session's own telemetry at it:

```sh
CLAUDE_CODE_ENABLE_TELEMETRY=1 \
OTEL_LOGS_EXPORTER=otlp \
OTEL_EXPORTER_OTLP_PROTOCOL=http/json \
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:8787/otlp \
OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer local-test-owner-token" \
OTEL_RESOURCE_ATTRIBUTES=service.name=claude-code \
OTEL_LOG_RAW_API_BODIES=1 \
claude -p "say hi"
```

A session shows up within a few seconds. Run the test suite with `uv run pytest` and
lint with `uv run ruff check .`.

## Usage

### The easy way: one command

Sign in at `/auth/login` (locally, or on the live demo) and the page hands you a single
command to paste into a terminal. It writes the MCP connection and telemetry config into
your own `~/.claude/settings.json` (backed up first, merged, never overwritten):

```sh
curl -fsSL https://ctxwindow.uk/setup/install?t=<code> | sh
```

The `?t=` code is single-use and short-lived, so your real token is never in the command
itself. Not comfortable piping into a shell? The same page has a download-then-inspect
variant of the identical script. Close and reopen Claude Code afterward, since env vars
only load at process startup, then run one prompt and check "Test my connection" on the
page.

### Claude Code CLI, by hand

```json
{
  "mcpServers": {
    "context-inspector": {
      "url": "https://ctxwindow.uk/mcp",
      "headers": { "Authorization": "Bearer <your-token>" }
    }
  }
}
```

### claude.ai chat (Connectors)

claude.ai's Connectors feature speaks the MCP OAuth flow directly, so there's no token
to copy:

1. **claude.ai → Settings → Connectors → Add custom connector.**
2. Paste the MCP server URL (`https://ctxwindow.uk/mcp`).
   Leave OAuth Client ID/Secret blank; this server registers itself dynamically per the
   MCP spec.
3. claude.ai opens a Google sign-in prompt automatically. Sign in once.
4. All 8 tools are now available in any claude.ai chat.

This connects claude.ai only. Claude Code CLI needs its own token from `/auth/login`,
even with the same Google account (see [Auth model](#auth-model) for why the two are
separate).

Once connected, ask it things like:

- *"What did my last Claude Code session cost?"*
- *"Show me the tool-call trace for session `sess_...`"*
- *"Which of my recent sessions used the most tokens?"*
- *"What's in the system prompt block for my current session's context window?"*

### Anthropic Messages API / MCP Connector

```json
{
  "mcp_servers": [{"type": "url", "url": "https://ctxwindow.uk/mcp", "name": "context-inspector", "authorization_token": "<your-token>"}],
  "tools": [{"type": "mcp_toolset", "mcp_server_name": "context-inspector"}]
}
```

Needs the `anthropic-beta: mcp-client-2025-11-20` header.

### Live telemetry: Claude Code / Copilot OTEL, by hand

Point Claude Code's or Copilot's own OpenTelemetry export at this server and sessions
show up as they happen. No wrapping your agent loop, no `record_session` calls, just
env vars. The connect page generates both snippets, pre-filled with your endpoint and
token:

```sh
# Claude Code
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_LOGS_EXPORTER=otlp
export OTEL_METRICS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
export OTEL_EXPORTER_OTLP_ENDPOINT=https://ctxwindow.uk/otlp
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer <your-token>"
export OTEL_RESOURCE_ATTRIBUTES=service.name=claude-code
export OTEL_METRICS_INCLUDE_SESSION_ID=true
export OTEL_LOGS_INCLUDE_SESSION_ID=true
export OTEL_LOGS_EXPORT_INTERVAL=5000
export OTEL_LOG_RAW_API_BODIES=1   # opt-in: needed for the Context Explorer

# GitHub Copilot
export COPILOT_OTEL_ENABLED=true
export OTEL_EXPORTER_OTLP_ENDPOINT=https://ctxwindow.uk/otlp
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer <your-token>"
export COPILOT_OTEL_CAPTURE_CONTENT=true   # opt-in: needed for the Context Explorer
```

The `OTEL_RESOURCE_ATTRIBUTES`/`*_INCLUDE_SESSION_ID` lines are what let the server
recognize the session as Claude Code's at all. Omit them and every session is silently
dropped. The `_RAW_API_BODIES`/`_CAPTURE_CONTENT` flags are separate opt-ins because they
carry full prompt/response content, not just metrics. When on, captured bodies pass
through a basic redaction layer (email addresses, home-directory paths) before storage.
Treat that as a trust reducer, not comprehensive PII scrubbing.

## The 8 MCP tools

| Tool | Returns | Read/write |
|---|---|---|
| `get_session_metrics` | Session metadata + per-prompt tokens/latency/cost | Read |
| `get_token_breakdown` | Per-turn token/latency breakdown | Read |
| `get_tool_metrics` | Tool call counts by status | Read |
| `get_agent_trace` | Ordered tool-call sequence for one session | Read |
| `get_cost_estimate` | Estimated cost, one session or a time window | Read |
| `get_recent_sessions` | Most recent sessions, newest first | Read |
| `get_context_timeline` | Full context-window block breakdown | Read |
| `record_session` | Records one agent execution's metrics | Write |

Plain REST equivalents are exposed under `/api/*`. See [Architecture](#architecture).

## Auth model

Two separate token surfaces exist by design. A plain bearer token (the owner token, or a
personal token from `/auth/login`) works for clients you hand a token to directly, such
as Claude Code's MCP config or curl. claude.ai's Connectors UI instead speaks the MCP
spec's OAuth 2.1 flow and mints its own token on sign-in, kept separate so disconnecting
one doesn't invalidate the other.

Every session is attributed to whoever recorded it. A per-user token only ever sees,
queries, and records its own data; there's no way to read or list another user's
sessions, even by guessing an ID. The owner token sees everyone's data, since it's your
server. Revoke someone's access with `mcp_server.auth.store.revoke(google_sub)` (find
their `sub` via `list_users()`). Their existing token stops working immediately, and
already-recorded data stays where it is.

### Google sign-in setup (one-time, about 2 minutes)

1. [console.cloud.google.com](https://console.cloud.google.com): create or pick a
   project, then go to **APIs & Services → Credentials**.
2. **Create Credentials → OAuth client ID** → Application type **Web application**.
3. Under **Authorized JavaScript origins**, add `http://localhost:8787` for local use,
   plus your real domain once deployed. Use `localhost`, not `127.0.0.1`: Google
   Identity Services only reliably honors `localhost` for the plain-HTTP local-dev
   exemption. No redirect URI needed.
4. Copy the **Client ID** (safe to expose client-side) and set it as
   `GOOGLE_OAUTH_CLIENT_ID`.

Switching `STORAGE_BACKEND` on a running deployment starts the auth store over empty.
Every existing token, including your own, stops working until you sign in again. That's
not a bug; the token store is only as durable as its backend.

### Letting a friend's own agent record its own data

`record_session` is also an authenticated MCP tool (and `/api/record-session` REST
route), so a friend's agent running anywhere can push its own sessions in, attributed to
them:

```python
import httpx

httpx.post(
    "http://<your-host>:8787/api/record-session",
    headers={"Authorization": f"Bearer {their_token}"},
    json={"prompt": prompt, "model_id": model_id, "loop_result": loop_result},
)
```

## Architecture

<p align="center">
  <img src="assets/architecture.svg" alt="Claude Code, browser clients, and Bedrock-style agents all talk through a Cloudflare Worker reverse proxy to a Cloud Run service, which reads and writes Firestore or DynamoDB" width="720">
</p>

Four pieces, front to back:

1. **Cloudflare Worker** (`cloudflare-proxy/worker.js`): a pass-through reverse proxy
   that gives the service a short permanent public URL (`ctxwindow.uk`) instead of the
   raw `.run.app` one. It rewrites nothing but the `Host` header and streams responses
   back untouched, so SSE endpoints work through it.
2. **Starlette + MCP server** (`mcp_server/`), running on Cloud Run. Serves the MCP
   handshake at `/mcp`, plain REST at `/api/*`, OTLP ingestion at `/otlp/v1/*`, Google
   sign-in at `/auth/login`, the one-line installer at `/setup/install`, the dashboard,
   and the docs site at `/docs`.
3. **Data-access layer** (`metrics/store.py`), one dispatcher in front of three
   interchangeable backends.
4. **Storage**: SQLite, DynamoDB, or Firestore, picked by `STORAGE_BACKEND`. See
   [Storage backends](#storage-backends).

Data reaches the store by three paths, and every read comes back out through the same
owner-filtered layer:

- A **direct Python import** of `metrics.store` for an agent loop running on the same
  machine.
- The authenticated **`record_session` MCP tool or `POST /api/record-session`** for
  anyone else's remote agent.
- **`/otlp/v1/{logs,metrics,traces}`** for Claude Code's and Copilot's own native
  OpenTelemetry export, parsed by `mcp_server/otlp/` into the same turns and tool calls.

```mermaid
flowchart LR
    subgraph Agent["Your agent loop"]
        A[run_agent_loop] --> R[record_session]
    end

    subgraph Package["CtxWindow"]
        R -->|"direct Python import\n(local, owner=None)"| S[metrics/store.py]
        MCP["mcp_server/routes/\n(MCP + REST routes)"] --> S
        S --> SQ["store_sqlite.py\n(local dev + public demo)"]
        S --> DY["store_dynamodb.py\n(AWS deployments)"]
        S --> FS["store_firestore.py\n(recommended for Cloud Run)"]
    end

    subgraph Remote["Another user's own agent"]
        FR[Their run_agent_loop] -->|"record_session tool /\nPOST /api/record-session"| MCP
    end

    Client["Bedrock-based agents / Claude Code"] -->|"Streamable HTTP,\nBearer token"| MCP

    subgraph OTLPClients["Claude Code / Copilot's own telemetry"]
        CC["Claude Code"] -->|"POST /otlp/v1/logs"| OTLP
        GHC["GitHub Copilot"] -->|"POST /otlp/v1/traces"| OTLP
    end
    OTLP["mcp_server/otlp/"] -->|"append_turn / append_tool_call"| S
```

`record_session(prompt, model_id, loop_result, owner=None)` needs `loop_result` shaped
like:

```python
{
    "trace": [{"tool": "...", "args": {...}, "status": "ok"}, ...],
    "turns": [{"input_tokens": int, "output_tokens": int, "latency_ms": int}, ...],
    "input_tokens": int, "output_tokens": int, "total_tokens": int, "latency_ms": int,
    "context_blocks": [   # optional: omit and you just lose the Explorer, nothing crashes
        {"category": "system", "label": "...", "char_count": int, "token_estimate": int, "turn_n": int | None},
        ...
    ],
}
```

`context_blocks` categories: `system`, `tools`, `user`, `reasoning`, `thinking`,
`tool_call`, `tool_result` (optional `"status"` field for color-coding failures), and
`answer`.

## Storage backends

Three interchangeable backends, selected with `STORAGE_BACKEND`. They expose identical
function signatures, so callers import from `metrics/store.py` and never know which one
is active.

- **`sqlite`** (default): zero setup, one file on disk, what local dev uses. Path is
  overridable with `METRICS_DB_PATH` so a container can write to a scratch location like
  `/tmp` instead of the repo's `data/` dir. A container's local filesystem doesn't
  persist, so anything written here is lost on a cold start. That's fine for local dev
  and for the public demo, and wrong for anything you care about keeping.
- **`dynamodb`**: durable, AWS-hosted, for a deployment already living in AWS. Table name
  from `METRICS_TABLE`, region from `AWS_REGION`. Single-table design: partition key
  `session_id`, sort key `sk` distinguishing item type (`SESSION`, `TURN#0000`,
  `TOOLCALL#0000`). Aggregate reads (recent sessions, aggregate tool metrics) use `Scan`,
  which its own docstring flags as fine at personal-project scale and worth revisiting
  with a GSI if that stops being true.
- **`firestore`**: durable, GCP-hosted, and the recommended choice for a real Cloud Run
  deployment with sign-in-backed writes, since the service is already on GCP. This is the
  backend this project's own code treats as its deployed target; the public demo at
  `ctxwindow.uk` deliberately stays on seeded SQLite instead, so a cold start wipes
  visitor writes back to the fixture data. A top-level `sessions`
  collection with `owner` and `timestamp` as directly queryable top-level fields, plus
  `turns`, `tool_calls`, and `context_blocks` subcollections. Because a real query is
  available here, it avoids the scan-then-filter tradeoff DynamoDB's backend accepts.
  The client uses Application Default Credentials, which works automatically on Cloud Run
  via its service account; grant that account `roles/datastore.user`. Needs a composite
  index on `(owner ASC, timestamp DESC)` on the `sessions` collection, created ahead of
  time via the Firestore console or `gcloud firestore indexes composite create`, rather
  than relying on the first-query error link in prod. Collection name is overridable with
  `METRICS_FIRESTORE_COLLECTION`. To test locally, run `gcloud emulators firestore start`
  and set `FIRESTORE_EMULATOR_HOST`; the client library honors it transparently.

The per-user auth token store (`mcp_server/auth/store.py`) reads the same
`STORAGE_BACKEND` variable, so sessions and auth always switch together. It needs the
same durability: under SQLite, a token row lost on cold start silently breaks that user's
auth with no error anywhere useful.

## Deploying your own (optional)

**You probably don't need this.** [ctxwindow.uk](https://ctxwindow.uk) is live, free, and
signs you in with Google, and your sessions are yours alone (see
[Auth model](#auth-model)). Running your own copy only makes sense if you want the data
on infrastructure you control, or you're changing the server itself.

If you do want your own: the deploy path this repo uses is a Cloud Run service built from
the `Dockerfile` at the repo root, fronted by the Cloudflare Worker in
`cloudflare-proxy/`. `.github/workflows/deploy.yml` runs it end to end on every push to
`main` that touches the server, and is the honest reference for the real steps:

1. Run the unit suite; a red suite blocks the deploy.
2. Authenticate to GCP with Workload Identity Federation, so there are no stored GCP keys
   in repo secrets. This needs the `GCP_PROJECT`, `GCP_WIF_PROVIDER`, and `GCP_DEPLOY_SA`
   repo variables set.
3. `docker buildx build --platform linux/amd64` and push to Artifact Registry. The image
   installs from `uv.lock` with `uv sync --frozen`, so builds stay reproducible.
4. `gcloud run deploy`, then `gcloud run services update-traffic --to-latest`. The second
   command is not redundant: `deploy` alone creates a revision but won't move traffic if a
   split was ever set out-of-band, which pinned production to a stale revision for two
   days here before it was caught.
5. Smoke-test `/health`, then `npx wrangler deploy` the Worker (needs the
   `CLOUDFLARE_API_TOKEN` secret and `CLOUDFLARE_ACCOUNT_ID` variable) and smoke-test it
   through the public hostname.

Forking this means replacing the GCP project, the Artifact Registry path in the
workflow's `IMAGE`, and `cloudflare-proxy/wrangler.toml`'s `ORIGIN` and `routes`, all of
which currently point at this project's own account and domain.

Relevant env vars once deployed:

- `PUBLIC_ORIGIN`: the real public origin (e.g.
  `https://ctxwindow.uk`), needed because behind the proxy
  `request.base_url` reflects Cloud Run's internal origin, not what a real caller used.
  Every URL ctxwindow generates about itself (OAuth metadata, the install command) needs the
  real one.
- `CHAT_UI_ORIGIN`: comma-separated CORS allowlist.
- `MCP_ALLOWED_HOSTS`: comma-separated `Host` header allowlist for the MCP SDK's
  DNS-rebinding protection. Getting this wrong causes `421 Invalid Host header` on
  authenticated requests only, so an unauthenticated smoke test won't catch it.
- `DEV_MODE_SUBS`: comma-separated Google `sub` allowlist for developer-mode dashboard
  features (currently: showing `api_tests`' synthetic probe sessions, hidden from
  everyone else by default). Find your own `sub` via `mcp_server.auth.store.list_users()`.
- `STORAGE_BACKEND`: `sqlite`, `dynamodb`, or `firestore`. Anything durable in practice.
- `DEMO_SEED_SRC`: path to a prebuilt SQLite demo dataset (`demo/metrics.db`, built by
  `scripts/seed_demo_db.py`). Set alongside a scratch `METRICS_DB_PATH` and the server
  copies the seed in on first boot only, which is how the public demo resets itself on a
  cold start. Leave both unset for a normal deployment.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for lint, tests, and what a good PR looks like
here.

## Roadmap

Shipped:

- [x] Curl-pipeable one-line setup (`/setup/install`)
- [x] Owner-scoped `/otlp/debug` + "Test your connection" panel
- [x] Route-enumerating tenant-isolation test
- [x] Firestore backend, and the production move onto it
- [x] Own domain (`ctxwindow.uk`) in front of the Worker proxy

Still open, honestly:

- [ ] **Windows/PowerShell installer variant.** `/setup/install` serves a POSIX shell
      script, so Windows users currently have to configure `settings.json` by hand.
- [ ] **Per-device token revoke.** Today revoking a user (`auth.store.revoke`) kills every
      token they hold at once; there's no way to drop one machine and keep the rest.
- [ ] **Publish to PyPI.** Install is still git-clone-only.
- [ ] **Cursor MCP tool access.** Worth being precise: Cursor's OTel telemetry export is
      Enterprise-only with no self-service config surface, so cost and token dashboards
      for Cursor sessions are not on the table. Pointing `local_setup.py`'s backup-and-
      merge logic at `~/.cursor/mcp.json` to expose the MCP tools there is feasible, and
      would be labeled "tool access," not "Cursor support."
- [ ] **A real per-project settings endpoint.** The dashboard's project settings panel is
      wired to placeholder state (`TODO` in `mcp_server/routes/auth.py`).
- [ ] **Redaction hardening.** The OTLP redaction layer catches email addresses and
      home-directory paths. That is a trust reducer, not comprehensive PII scrubbing, and
      it's documented that way on purpose.

## Questions or bugs?

Bugs, feature requests, and anything reproducible belong in
[GitHub issues](https://github.com/sohaibsohail98/mcp-context-inspector/issues), which is
where they'll actually get tracked. See
[CONTRIBUTING.md](CONTRIBUTING.md#reporting-a-bug) for what makes a good report. For a
security issue, please don't open a public issue; open a
[private security advisory](https://github.com/sohaibsohail98/mcp-context-inspector/security/advisories/new)
instead.

For anything that doesn't feel like an issue, a question about how something works, or
whether a use case is a fit, there's a prefilled starting point here:

[Ask a question](https://github.com/sohaibsohail98/mcp-context-inspector/issues/new?title=ctxwindow%3A%20question&labels=question)

## License

MIT licensed; see [LICENSE](LICENSE). Developed alongside
[`sre-investigation-agent`](https://github.com/sohaibsohail98/sre-investigation-agent),
the reference chat UI and Bedrock agent this package was extracted from.
