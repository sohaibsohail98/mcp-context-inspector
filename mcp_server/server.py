"""MCP server for the SRE agent's execution metrics — its own thing,
decoupled from agent/. Exposes 7 read-only tools plus one write tool
(record_session, for a caller's own remote agent to push its own data
in) over Streamable HTTP, plus a few plain REST routes (curl-friendly
alternative to a real MCP handshake — see web/mcp-client.js for the
actual protocol client). Both paths call the same underlying functions
in metrics/store.py — one data-access layer, not two implementations of
"how do I read a session."

Reusable, connectable by any agent — not coupled to any specific chat UI.
See web/server.py for the chat frontend, a separate process.

Run from repo root:
    uv run python -m mcp_server.server
"""

import contextvars
import os
import shutil

from mcp.server.mcpserver import MCPServer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from mcp_server import auth_store
from mcp_server.google_auth import InvalidGoogleToken, verify_credential
from metrics import store

server = MCPServer(name="sre-agent-metrics")

# Set by MultiTokenAuthMiddleware once per request, read by every tool/REST
# handler below — the caller's Google `sub` if they connected with a
# per-user token, or None if they connected with the owner token (which
# sees everything, "it's your server" semantics). A plain contextvar
# rather than threading it through every function signature: Starlette's
# BaseHTTPMiddleware runs the downstream handler in the same asyncio
# task, so a value set here before call_next() is visible inside the
# request it wraps, including MCP tool dispatch — see
# tests/test_mcp_protocol_ownership.py for a real MCP `tools/call` test
# confirming this.
current_owner = contextvars.ContextVar("current_owner", default=None)


class MultiTokenAuthMiddleware(BaseHTTPMiddleware):
    """Gates /mcp and /api/ behind a bearer token that's either the
    owner's single shared token (`owner_token`, printed to stdout on
    startup) or a per-user token minted via /auth/login and /auth/verify
    (`auth_store.is_valid_token`). /api/ is protected too, since those
    routes return the same session data as the MCP tools; leaving them
    open would make the MCP-side auth pointless. /auth/* itself stays
    unauthenticated, since that is the pre-auth sign-in flow.

    Also sets `current_owner` for the rest of this request, so every
    read/write below can filter to the caller's own data. See
    metrics/store.py's owner param and the README's Auth section."""

    def __init__(self, app, owner_token, protected_prefixes=("/mcp", "/api/")):
        super().__init__(app)
        self.owner_token = owner_token
        self.protected_prefixes = protected_prefixes

    async def dispatch(self, request, call_next):
        if any(request.url.path.startswith(p) for p in self.protected_prefixes):
            header = request.headers.get("authorization", "")
            token = header.removeprefix("Bearer ") if header.startswith("Bearer ") else None
            if token == self.owner_token:
                current_owner.set(None)
            elif token and auth_store.is_valid_token(token):
                current_owner.set(auth_store.get_sub_for_token(token))
            else:
                return JSONResponse({"error": "unauthorized — missing or wrong bearer token"}, status_code=401)
        return await call_next(request)


@server.tool()
def get_session_metrics(session_id: str) -> dict:
    """Session metadata plus per-prompt metrics (tokens, latency, cost) for one investigation."""
    return store.get_session_metrics(session_id, owner=current_owner.get()) or {"error": "session not found"}


@server.tool()
def get_token_breakdown(session_id: str) -> list:
    """Per-turn token and latency breakdown for one session."""
    return store.get_token_breakdown(session_id, owner=current_owner.get())


@server.tool()
def get_tool_metrics(session_id: str | None = None) -> list:
    """Tool call counts by status — for one session, or aggregated across all (aggregated across
    only your own sessions if you're not the server owner)."""
    return store.get_tool_metrics(session_id, owner=current_owner.get())


@server.tool()
def get_agent_trace(session_id: str) -> list:
    """The ordered sequence of tool calls (name, args, status) for one session."""
    return store.get_agent_trace(session_id, owner=current_owner.get())


@server.tool()
def get_cost_estimate(session_id: str | None = None, period_seconds: int | None = None) -> float:
    """Estimated cost for one session, or summed over the last period_seconds (your own
    sessions only, unless you're the server owner)."""
    return store.get_cost_estimate(session_id, period_seconds, owner=current_owner.get())


@server.tool()
def get_recent_sessions(limit: int = 10) -> list:
    """The most recent investigation sessions, newest first — your own only, unless
    you're the server owner (owner token), who sees everyone's."""
    return store.get_recent_sessions(limit, owner=current_owner.get())


@server.tool()
def get_context_timeline(session_id: str) -> list:
    """Ordered, categorized breakdown of everything that entered this
    session's context window, with cumulative token estimates —
    character-based estimates for composition, not exact Bedrock usage
    numbers (see get_session_metrics for those)."""
    return store.get_context_timeline(session_id, owner=current_owner.get())


@server.tool()
def record_session(prompt: str, model_id: str, loop_result: dict) -> str:
    """Records one agent execution's metrics, attributed to whoever's
    connected (your own Google account, if you signed in via
    /auth/login — the owner token records as owner=None, same as the
    local direct-import path `from metrics.store import record_session`
    used by this server's own reference agent). This is how a friend's
    own remote agent gets its sessions into this server at all, rather
    than only being able to query data the server owner recorded
    locally. loop_result must have the shape agent/runtime.py's
    run_agent_loop() returns — see this package's README for the exact
    contract. Returns the new session_id."""
    return store.record_session(prompt, model_id, loop_result, owner=current_owner.get())


@server.custom_route("/api/sessions", methods=["GET"])
async def api_sessions(request: Request):
    limit = int(request.query_params.get("limit", 10))
    return JSONResponse(store.get_recent_sessions(limit, owner=current_owner.get()))


@server.custom_route("/api/sessions/{session_id}", methods=["GET"])
async def api_session_detail(request: Request):
    session_id = request.path_params["session_id"]
    owner = current_owner.get()
    metrics = store.get_session_metrics(session_id, owner=owner)
    if metrics is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(
        {
            "metrics": metrics,
            "turns": store.get_token_breakdown(session_id, owner=owner),
            "trace": store.get_agent_trace(session_id, owner=owner),
        }
    )


@server.custom_route("/api/tool-metrics", methods=["GET"])
async def api_tool_metrics(request: Request):
    return JSONResponse(store.get_tool_metrics(owner=current_owner.get()))


@server.custom_route("/api/cost", methods=["GET"])
async def api_cost(request: Request):
    period = request.query_params.get("period_seconds")
    period = int(period) if period else None
    return JSONResponse({"total_cost": store.get_cost_estimate(period_seconds=period, owner=current_owner.get())})


@server.custom_route("/api/context-timeline/{session_id}", methods=["GET"])
async def api_context_timeline(request: Request):
    session_id = request.path_params["session_id"]
    return JSONResponse(store.get_context_timeline(session_id, owner=current_owner.get()))


@server.custom_route("/", methods=["GET"])
async def root_redirect(request: Request):
    """This is a headless MCP server, not a browsable app — visiting the
    bare URL with no route registered here would otherwise 404 with a
    blank page, which reads as "broken" to anyone who just opens the
    service URL. /auth/login is the actual human entry point (sign-in +
    connection instructions), same reasoning as web/server.py's own
    root redirect to /chat."""
    return RedirectResponse(url="/auth/login")


@server.custom_route("/health", methods=["GET"])
async def healthz(request: Request):
    """Unauthenticated — used by Cloud Scheduler to keep the deployed
    instance warm, and by anyone checking the service is up at all."""
    return JSONResponse({"status": "ok"})


def _maybe_seed_demo_db(demo_seed_src, target_path):
    """Public demo deployment: the image bakes a read-only demo dataset
    (DEMO_SEED_SRC, e.g. demo/metrics.db, built by scripts/seed_demo_db.py)
    but METRICS_DB_PATH points writes at an ephemeral path (e.g. /tmp) so
    a signed-in visitor's own record_session calls don't touch the image
    layer. Copies the seed in on first boot only — a cold start resets it,
    which is the documented tradeoff, not a bug. No-op if demo_seed_src
    isn't set or target_path already exists."""
    if not demo_seed_src or target_path.exists():
        return False
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(demo_seed_src, target_path)
    return True


def _resolve_owner_token(env_value):
    """.strip(): Secret Manager values are frequently created via
    `echo "token" | gcloud secrets create ...`, which appends a trailing
    newline that becomes part of the mounted env var — an untrimmed
    comparison in MultiTokenAuthMiddleware would then reject every real
    request carrying the actual token. Returns None for unset/blank,
    same as no MCP_AUTH_TOKEN configured."""
    return (env_value or "").strip() or None


@server.custom_route("/api/record-session", methods=["POST"])
async def api_record_session(request: Request):
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "malformed JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    try:
        prompt, model_id, loop_result = body["prompt"], body["model_id"], body["loop_result"]
    except KeyError as e:
        return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
    session_id = store.record_session(prompt, model_id, loop_result, owner=current_owner.get())
    return JSONResponse({"session_id": session_id})


# Google sign-in: the pre-auth flow that mints a per-user MCP token; not
# gated by MultiTokenAuthMiddleware, which only checks /mcp and /api/.


_PAGE_STYLE = """
  :root {
    --bg: #0a0d13; --bg-raised: #12161f; --bg-raised-2: #161b26;
    --border: #232a38; --border-soft: #1b212d;
    --text: #e4eaf3; --text-dim: #8b96a8; --text-dimmer: #5f6b7d;
    --accent: #35e0c8; --accent-2: #6c8dff; --accent-dim: #163832;
    --warn: #f5b955; --warn-dim: #3a2c14; --warn-border: #4a3a1a;
    --shadow: 0 1px 2px rgba(0,0,0,0.3), 0 12px 32px -12px rgba(0,0,0,0.55);
    --radius: 16px;
  }
  * { box-sizing: border-box; }
  html { background: var(--bg); }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", sans-serif;
    max-width: 640px; margin: 0 auto; padding: 4.5rem 1.5rem 5rem;
    background:
      radial-gradient(1200px 480px at 50% -10%, rgba(53,224,200,0.10), transparent 60%),
      var(--bg);
    color: var(--text); line-height: 1.6; -webkit-font-smoothing: antialiased;
  }
  .brand { display: flex; align-items: center; gap: 0.65rem; margin-bottom: 1.1rem; }
  .brand-mark {
    display: flex; align-items: center; justify-content: center;
    width: 2.1rem; height: 2.1rem; border-radius: 9px; flex-shrink: 0;
    background: linear-gradient(155deg, var(--accent), var(--accent-2));
    color: #06110f; font-size: 1.05rem; font-weight: 700;
    box-shadow: 0 4px 16px -4px rgba(53,224,200,0.45);
  }
  h1 {
    font-size: 1.55rem; margin: 0; letter-spacing: -0.015em; font-weight: 650;
  }
  h3 { margin-top: 0; font-size: 0.98rem; color: var(--text); font-weight: 600; letter-spacing: -0.005em; }
  .sub { color: var(--text-dim); margin-top: 0; margin-bottom: 2rem; font-size: 1rem; max-width: 34rem; }
  .card {
    border: 1px solid var(--border); background: var(--bg-raised);
    border-radius: var(--radius); padding: 1.5rem 1.6rem; margin: 1rem 0;
    box-shadow: var(--shadow);
  }
  .card.security { border-color: var(--warn-border); background: linear-gradient(180deg, var(--warn-dim), var(--bg-raised) 60%); }
  .card.accent { border-color: rgba(53,224,200,0.28); background: linear-gradient(165deg, var(--accent-dim), var(--bg-raised) 65%); }
  .card-hint { margin-top: 0; color: var(--text-dim); font-size: 0.88rem; }
  ul.features { list-style: none; padding-left: 0; margin: 0.9rem 0 0; display: grid; gap: 0.75rem; }
  ul.features li { position: relative; padding-left: 1.3rem; font-size: 0.94rem; color: var(--text); }
  ul.features li::before {
    content: ""; position: absolute; left: 0; top: 0.55rem; width: 6px; height: 6px; border-radius: 999px;
    background: var(--accent); box-shadow: 0 0 0 3px var(--accent-dim);
  }
  code, pre {
    background: #060a0f; border: 1px solid var(--border-soft); border-radius: 8px;
    color: #9be8db; font-family: ui-monospace, "SF Mono", Menlo, monospace;
  }
  code { padding: 0.18rem 0.5rem; font-size: 0.85em; border-width: 0; background: #10161f; }
  pre { padding: 1rem 1.1rem; overflow-x: auto; font-size: 0.8rem; white-space: pre-wrap; word-break: break-all; }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  button.copy {
    font-size: 0.78rem; font-weight: 500; padding: 0.4rem 0.85rem; border-radius: 8px;
    border: 1px solid var(--border); background: var(--bg-raised-2); color: var(--text);
    cursor: pointer; margin-left: 0.4rem; margin-top: 0.65rem;
    transition: border-color 0.15s ease, color 0.15s ease, background 0.15s ease;
  }
  button.copy:hover { border-color: var(--accent); color: var(--accent); background: var(--accent-dim); }
  .hidden { display: none; }
  details {
    border: 1px solid var(--border); border-radius: 12px; padding: 0.85rem 1.1rem;
    margin: 0.6rem 0; background: var(--bg-raised); transition: border-color 0.15s ease;
  }
  details:hover { border-color: #2c3547; }
  details summary {
    cursor: pointer; font-size: 0.9rem; color: var(--text-dim); font-weight: 500;
    list-style: none; display: flex; align-items: center; gap: 0.6rem;
  }
  details summary::-webkit-details-marker { display: none; }
  details summary::before {
    content: "›"; color: var(--accent); font-size: 1.1rem; line-height: 1;
    display: inline-block; transition: transform 0.18s ease; transform: rotate(0deg);
  }
  details[open] summary::before { transform: rotate(90deg); }
  details[open] summary { color: var(--text); }
  details p { margin-bottom: 0; margin-top: 0.7rem; font-size: 0.87rem; color: var(--text-dim); padding-left: 1.7rem; }
  .badge {
    display: inline-block; font-size: 0.72rem; font-weight: 600; padding: 0.2rem 0.6rem; border-radius: 999px;
    background: var(--accent-dim); color: var(--accent); margin-left: 0.5rem; vertical-align: middle;
  }
  .g_id_signin { margin-top: 0.35rem; }

  /* Consent (Authorize/Cancel) + success confirmation — modeled after
     Cloudflare Wrangler's OAuth "wants to access your account" and
     "Authorization granted" screens. */
  .handshake { display: flex; align-items: center; justify-content: center; gap: 1rem; margin: 0.25rem 0 1.75rem; }
  .icon-circle {
    width: 3.4rem; height: 3.4rem; border-radius: 999px; flex-shrink: 0; position: relative;
    display: flex; align-items: center; justify-content: center; font-size: 1.4rem;
    background: var(--bg-raised-2); border: 1px solid var(--border);
  }
  .icon-circle.accent { background: linear-gradient(155deg, var(--accent), var(--accent-2)); color: #06110f; }
  .handshake .arrow { color: var(--text-dimmer); font-size: 1.3rem; }
  .badge-check {
    position: absolute; bottom: -2px; right: -2px; width: 1.15rem; height: 1.15rem; border-radius: 999px;
    background: var(--accent); color: #06110f; display: flex; align-items: center; justify-content: center;
    font-size: 0.72rem; font-weight: 700; border: 2px solid var(--bg);
  }
  .consent-title { text-align: center; font-size: 1.28rem; font-weight: 650; margin: 0 0 0.4rem; letter-spacing: -0.01em; }
  .consent-sub { text-align: center; color: var(--text-dim); font-size: 0.9rem; margin: 0 0 1.5rem; }
  .identity-row {
    display: flex; align-items: center; gap: 0.65rem; padding: 0.7rem 0.9rem; margin-bottom: 1.25rem;
    border: 1px solid var(--border); border-radius: 10px; background: var(--bg-raised-2); font-size: 0.87rem; color: var(--text-dim);
  }
  .identity-row .avatar {
    width: 1.6rem; height: 1.6rem; border-radius: 999px; flex-shrink: 0; display: flex; align-items: center;
    justify-content: center; font-size: 0.74rem; font-weight: 700; color: #06110f;
    background: linear-gradient(155deg, var(--accent), var(--accent-2));
  }
  .permission-list { display: grid; gap: 0.65rem; margin: 0 0 1.5rem; }
  .permission-row { display: flex; align-items: flex-start; gap: 0.7rem; font-size: 0.9rem; color: var(--text); }
  .permission-row .dot {
    flex-shrink: 0; width: 6px; height: 6px; border-radius: 999px; margin-top: 0.5rem;
    background: var(--accent); box-shadow: 0 0 0 3px var(--accent-dim);
  }
  .btn-row { display: flex; gap: 0.7rem; }
  .btn-primary, .btn-secondary {
    flex: 1; text-align: center; padding: 0.7rem 1rem; border-radius: 10px; font-weight: 600;
    font-size: 0.9rem; cursor: pointer; transition: filter 0.15s ease, border-color 0.15s ease, color 0.15s ease;
  }
  .btn-primary { border: none; background: linear-gradient(155deg, var(--accent), var(--accent-2)); color: #06110f; }
  .btn-primary:hover { filter: brightness(1.08); }
  .btn-secondary { border: 1px solid var(--border); background: transparent; color: var(--text-dim); font-weight: 500; }
  .btn-secondary:hover { border-color: #3a4459; color: var(--text); }
  .success-banner { display: flex; align-items: center; gap: 0.9rem; margin-bottom: 1.5rem; }
  .success-banner .icon-circle { width: 2.7rem; height: 2.7rem; font-size: 1.15rem; }
  .success-banner h2 { margin: 0; font-size: 1.05rem; font-weight: 650; }
  .success-banner p { margin: 0.15rem 0 0; font-size: 0.85rem; color: var(--text-dim); }

  /* Post-connect: one compact "your connection" summary + a tabbed
     client picker, instead of five permanently-stacked code blocks. */
  .kv-row { display: flex; align-items: center; justify-content: space-between; gap: 0.75rem; padding: 0.55rem 0; border-bottom: 1px solid var(--border-soft); }
  .kv-row:last-child { border-bottom: none; }
  .kv-label { font-size: 0.78rem; color: var(--text-dim); flex-shrink: 0; }
  .kv-value {
    font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 0.78rem; color: #9be8db;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; text-align: right;
  }
  .tab-row { display: flex; gap: 0.4rem; flex-wrap: wrap; }
  .tab-btn {
    padding: 0.4rem 0.8rem; border-radius: 8px; border: 1px solid var(--border); background: var(--bg-raised-2);
    color: var(--text-dim); font-size: 0.8rem; font-weight: 500; cursor: pointer; transition: all 0.15s ease;
  }
  .tab-btn:hover { border-color: #3a4459; color: var(--text); }
  .tab-btn.active { background: var(--accent-dim); border-color: rgba(53,224,200,0.35); color: var(--accent); }
  .tab-panel { display: none; margin-top: 1rem; }
  .tab-panel.active { display: block; }

  /* Landing/home page hero */
  .hero { text-align: center; padding: 1rem 0 0.5rem; }
  .hero-eyebrow {
    display: inline-flex; align-items: center; gap: 0.4rem; font-size: 0.72rem; font-weight: 600;
    color: var(--accent); text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 0.9rem;
  }
  .hero-eyebrow::before { content: ""; width: 6px; height: 6px; border-radius: 999px; background: var(--accent); box-shadow: 0 0 0 3px var(--accent-dim); }
  .hero h1 { font-size: 1.9rem; margin: 0 0 0.6rem; }
  .hero .sub { max-width: 30rem; margin-left: auto; margin-right: auto; font-size: 1.02rem; }
  .preview-frame {
    position: relative; border-radius: 14px; border: 1px solid var(--border); background: #060a0f;
    overflow: hidden; margin: 1.5rem 0 2rem; box-shadow: var(--shadow);
  }
  .preview-frame .chrome {
    display: flex; align-items: center; gap: 0.4rem; padding: 0.65rem 0.9rem; border-bottom: 1px solid var(--border-soft); background: var(--bg-raised);
  }
  .preview-frame .chrome .dot { width: 0.55rem; height: 0.55rem; border-radius: 999px; background: #333c4c; flex-shrink: 0; }
  .preview-frame .chrome-url {
    margin-left: 0.6rem; font-family: ui-monospace, monospace; font-size: 0.7rem; color: var(--text-dimmer);
    background: var(--bg-raised-2); border-radius: 6px; padding: 0.15rem 0.6rem;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0;
  }
  .preview-body { padding: 1.1rem 1.2rem 1.3rem; }
  .preview-bar { display: flex; height: 10px; width: 100%; border-radius: 999px; overflow: hidden; background: var(--bg-raised-2); margin-bottom: 0.9rem; }
  .preview-legend { display: flex; flex-wrap: wrap; gap: 0.9rem; font-size: 0.72rem; color: var(--text-dim); margin-bottom: 1rem; }
  .preview-legend span { display: inline-flex; align-items: center; gap: 0.35rem; }
  .preview-legend i { width: 7px; height: 7px; border-radius: 999px; display: inline-block; }
  .preview-block {
    display: flex; align-items: center; justify-content: space-between; gap: 0.75rem;
    padding: 0.55rem 0.7rem; border-radius: 8px; background: var(--bg-raised-2); margin-bottom: 0.4rem; font-size: 0.78rem;
  }
  .preview-block .label { display: flex; align-items: center; gap: 0.55rem; color: var(--text); }
  .preview-block .label i { width: 7px; height: 7px; border-radius: 999px; flex-shrink: 0; }
  .preview-block .tok { font-family: ui-monospace, monospace; color: var(--text-dimmer); font-size: 0.72rem; }
  .byline { text-align: center; font-size: 0.82rem; color: var(--text-dimmer); margin: -1rem 0 2rem; }
  .byline a { color: var(--text-dim); }
"""


@server.custom_route("/auth/login", methods=["GET"])
async def auth_login(request: Request):
    intro = """
<div class="hero">
  <span class="hero-eyebrow">Model Context Protocol server</span>
  <h1>mcp-context-inspector</h1>
  <p class="sub">Execution metrics and a full Context Window Explorer for any tool-calling agent — over a real MCP server, not a mock.</p>
</div>

<div class="preview-frame">
  <div class="chrome">
    <span class="dot"></span><span class="dot"></span><span class="dot"></span>
    <span class="chrome-url">Context Window Explorer &middot; live preview</span>
  </div>
  <div class="preview-body">
    <div class="preview-bar">
      <div style="width:14%; background:#6b7280;"></div>
      <div style="width:22%; background:#8b98ac;"></div>
      <div style="width:6%; background:#e4eaf3;"></div>
      <div style="width:18%; background:var(--accent);"></div>
      <div style="width:12%; background:var(--warn);"></div>
      <div style="width:20%; background:#4ade80;"></div>
      <div style="width:8%; background:var(--accent-2);"></div>
    </div>
    <div class="preview-legend">
      <span><i style="background:#6b7280;"></i>system</span>
      <span><i style="background:#8b98ac;"></i>tools</span>
      <span><i style="background:var(--accent);"></i>reasoning</span>
      <span><i style="background:var(--warn);"></i>tool call</span>
      <span><i style="background:#4ade80;"></i>tool result</span>
      <span><i style="background:var(--accent-2);"></i>answer</span>
    </div>
    <div class="preview-block"><span class="label"><i style="background:var(--warn);"></i>Tool call: get_service_metrics</span><span class="tok">120 tok</span></div>
    <div class="preview-block"><span class="label"><i style="background:#4ade80;"></i>Tool result: get_service_metrics</span><span class="tok">640 tok</span></div>
    <div class="preview-block"><span class="label"><i style="background:var(--accent-2);"></i>Final answer</span><span class="tok">210 tok</span></div>
  </div>
</div>
<p class="byline">Built by <a href="https://github.com/sohaibsohail98" target="_blank" rel="noopener">@sohaibsohail98</a></p>

<div class="card">
  <h3>What this gives your LLM/agent</h3>
  <ul class="features">
    <li>Real per-session cost, token, and tool-call metrics — 7 read-only MCP tools</li>
    <li>The <strong>Context Window Explorer</strong> — exactly what entered the model's context window, block by block, with honest token estimates</li>
    <li>Your own data, isolated from anyone else connected to this server — sign in below and everything you record or query is scoped to your account</li>
  </ul>
</div>
<details>
  <summary>What is an MCP server?</summary>
  <p>MCP (Model Context Protocol) is an open standard that lets an LLM or agent call tools over a
  normal HTTP connection — the same handshake works whether you're connecting Claude, ChatGPT,
  Cursor, or your own custom agent. This server exposes read/write tools for agent execution
  data; nothing here is specific to any one AI provider.</p>
</details>
<details>
  <summary>Why sign in with Google instead of a password?</summary>
  <p>No account to create or password to remember here — Google verifies who you are, this
  server just checks the signed proof and hands you a token scoped to your account. That token,
  not your Google identity itself, is what your agent actually uses afterward.</p>
</details>
<details>
  <summary>What does "your own data" actually mean?</summary>
  <p>Every session recorded through your token is tagged with your account. Reads are filtered
  the same way — you only ever see, list, or query sessions you recorded. Even guessing another
  person's session ID reads back as "not found," identical to one that never existed.</p>
</details>
"""
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    if not client_id:
        return HTMLResponse(f"""<!doctype html>
<html><head><title>mcp-context-inspector</title><style>{_PAGE_STYLE}</style></head>
<body>{intro}
<div class="card security">
  <p>Google sign-in isn't configured on this server — <code>GOOGLE_OAUTH_CLIENT_ID</code>
  isn't set. Ask whoever's running it to set that up, or use the owner's shared token instead.</p>
</div>
</body></html>""", status_code=503)

    return HTMLResponse(f"""<!doctype html>
<html><head><title>mcp-context-inspector</title>
<style>{_PAGE_STYLE}</style>
<script src="https://accounts.google.com/gsi/client" async defer></script>
</head><body>
<div id="intro">{intro}
<div class="card">
  <h3>Sign in to get your token</h3>
  <p style="margin-top:0; color: var(--text-dim); font-size: 0.9rem;">One click — no password, no account to create here.</p>
  <div id="g_id_onload" data-client_id="{client_id}" data-callback="onSignIn"></div>
  <div class="g_id_signin" data-type="standard" data-theme="filled_black"></div>
</div>
</div>
<div id="landing" class="hidden"></div>
<script>
  const mcpUrl = window.location.origin + "/mcp";

  function connectPage(email, token) {{
    const claudeConfig = JSON.stringify({{
      mcpServers: {{
        "context-inspector": {{
          url: mcpUrl,
          headers: {{ Authorization: "Bearer " + token }}
        }}
      }}
    }}, null, 2);
    const vscodeConfig = JSON.stringify({{
      servers: {{
        "context-inspector": {{
          type: "http",
          url: mcpUrl,
          headers: {{ Authorization: "Bearer " + token }}
        }}
      }}
    }}, null, 2);
    const rawHeader = "Authorization: Bearer " + token;
    const curlCmd = 'curl -H "Authorization: Bearer ' + token + '" ' + window.location.origin + '/api/sessions';

    return `
      <div class="card accent">
        <h3>Your connection</h3>
        <div style="margin-top: 0.9rem;">
          <div class="kv-row"><span class="kv-label">MCP server URL</span><span class="kv-value">` + mcpUrl + `</span></div>
          <div class="kv-row"><span class="kv-label">Your token</span><span class="kv-value">` + token + `</span></div>
        </div>
        <p class="card-hint" style="margin-top: 0.8rem;">Keep your token private — anyone with it can read and record data as you.</p>
        <div style="display: flex; gap: 0.5rem; margin-top: 0.8rem;">
          <button class="copy" onclick="copyText('token-raw')">Copy token</button>
          <button class="copy" onclick="copyText('url-raw')">Copy URL</button>
        </div>
        <span id="token-raw" class="hidden">` + token + `</span>
        <span id="url-raw" class="hidden">` + mcpUrl + `</span>
      </div>

      <div class="card">
        <h3>Connect your client</h3>
        <div class="tab-row" style="margin-top: 0.9rem;">
          <button class="tab-btn active" data-tab="claude" onclick="showConnectTab('claude')">Claude Desktop</button>
          <button class="tab-btn" data-tab="vscode" onclick="showConnectTab('vscode')">VS Code</button>
          <button class="tab-btn" data-tab="webui" onclick="showConnectTab('webui')">Claude.ai / ChatGPT</button>
          <button class="tab-btn" data-tab="api" onclick="showConnectTab('api')">API / curl</button>
        </div>

        <div class="tab-panel active" data-panel="claude">
          <p class="card-hint">Add this to your client's MCP server config:</p>
          <pre id="claude-config">` + claudeConfig + `</pre>
          <button class="copy" onclick="copyText('claude-config')">Copy config</button>
        </div>
        <div class="tab-panel" data-panel="vscode">
          <p class="card-hint">Add this to <code>.vscode/mcp.json</code>:</p>
          <pre id="vscode-config">` + vscodeConfig + `</pre>
          <button class="copy" onclick="copyText('vscode-config')">Copy config</button>
        </div>
        <div class="tab-panel" data-panel="webui">
          <p class="card-hint">Add a custom connector and paste the MCP server URL above — no config file, no header to set by hand.</p>
        </div>
        <div class="tab-panel" data-panel="api">
          <p class="card-hint">Any other LLM/agent — point it at the MCP server URL above with this header on every request:</p>
          <pre id="raw-header">` + rawHeader + `</pre>
          <button class="copy" onclick="copyText('raw-header')">Copy header</button>
          <p class="card-hint" style="margin-top: 0.9rem;">curl (debugging):</p>
          <pre id="curl-cmd">` + curlCmd + `</pre>
          <button class="copy" onclick="copyText('curl-cmd')">Copy curl</button>
        </div>
      </div>

      <details>
        <summary>How do I record my own agent's sessions here, not just read?</summary>
        <p>Call the <code>record_session</code> MCP tool (or POST <code>/api/record-session</code>) with the same
        bearer token — whatever you record is automatically attributed to you, the same way reads are scoped.
        See the package README's "Letting a friend's own agent record its own data" section for the exact shape.</p>
      </details>
      <details>
        <summary>Can this token be revoked?</summary>
        <p>Yes — the server owner can revoke your access at any time; you'd just sign in again here for a new one.
        Your already-recorded data isn't deleted, and stays visible only to you and the server owner.</p>
      </details>
    `;
  }}

  function showConnectTab(name) {{
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.dataset.panel === name));
  }}

  function copyText(id) {{
    navigator.clipboard.writeText(document.getElementById(id).textContent);
  }}

  // Decodes a Google ID token's payload for DISPLAY only (email, in the
  // consent screen) — this is NOT verification. The signature is checked
  // server-side in /auth/verify, which is the only place this credential
  // is trusted for anything security-relevant.
  //
  // Duplicated verbatim in sre-investigation-agent's web/chat.js (same
  // function, same purpose, its own consent flow) — deliberately not
  // shared, since these are two different repos/origins with no build
  // step between them. Fix bugs in both copies.
  function decodeJwtPayloadForDisplay(token) {{
    try {{
      const base64 = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
      const json = decodeURIComponent(
        atob(base64).split("").map((c) => "%" + c.charCodeAt(0).toString(16).padStart(2, "0")).join("")
      );
      return JSON.parse(json);
    }} catch {{
      return {{}};
    }}
  }}

  let pendingCredential = null;

  function consentPage(email) {{
    const initial = (email || "?").trim()[0]?.toUpperCase() || "?";
    return `
      <div class="handshake">
        <div class="icon-circle">◈</div>
        <span class="arrow">┅┅┅&gt;</span>
        <div class="icon-circle accent">◈<span class="badge-check">✓</span></div>
      </div>
      <h1 class="consent-title">Connect to mcp-context-inspector</h1>
      <p class="consent-sub">This will mint a personal access token scoped to your account.</p>
      <div class="identity-row">
        <span class="avatar">` + initial + `</span>
        Signing in as ` + email + `
      </div>
      <div class="card">
        <h3>This will allow it to</h3>
        <div class="permission-list" style="margin-top: 0.9rem; margin-bottom: 0;">
          <div class="permission-row"><span class="dot"></span> Read session metrics, cost, and tool-call history you record</div>
          <div class="permission-row"><span class="dot"></span> Record new investigation sessions attributed to your account</div>
          <div class="permission-row"><span class="dot"></span> Nothing else — no access to anyone else's data, ever</div>
        </div>
      </div>
      <div class="btn-row" style="margin-top: 1.25rem;">
        <button class="btn-secondary" onclick="cancelConsent()">Cancel</button>
        <button class="btn-primary" onclick="authorize()">Authorize</button>
      </div>
    `;
  }}

  function successBanner(email) {{
    return `
      <div class="success-banner">
        <div class="icon-circle accent">◈<span class="badge-check">✓</span></div>
        <div>
          <h2>Authorization granted</h2>
          <p>Signed in as ` + email + ` — everything below is scoped to your account only.</p>
        </div>
      </div>
    `;
  }}

  function onSignIn(response) {{
    pendingCredential = response.credential;
    const {{ email }} = decodeJwtPayloadForDisplay(response.credential);
    document.getElementById("intro").classList.add("hidden");
    const landing = document.getElementById("landing");
    landing.classList.remove("hidden");
    landing.innerHTML = consentPage(email || "your Google account");
  }}

  function cancelConsent() {{
    pendingCredential = null;
    document.getElementById("landing").classList.add("hidden");
    document.getElementById("intro").classList.remove("hidden");
  }}

  async function authorize() {{
    if (!pendingCredential) return;
    const landing = document.getElementById("landing");
    landing.innerHTML = "<p class=\\"sub\\">Authorizing…</p>";
    try {{
      const res = await fetch("/auth/verify", {{
        method: "POST",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify({{credential: pendingCredential}}),
      }});
      const data = await res.json();
      pendingCredential = null;
      if (res.ok) {{
        landing.innerHTML = successBanner(data.email) + connectPage(data.email, data.mcp_token);
      }} else {{
        landing.innerHTML = "<div class='card security'>Sign-in failed: " + (data.error || "unknown error") + "</div>";
      }}
    }} catch (err) {{
      pendingCredential = null;
      landing.innerHTML = "<div class='card security'>Sign-in failed: " + err.message + "</div>";
    }}
  }}
</script>
</body></html>""")


@server.custom_route("/auth/verify", methods=["POST"])
async def auth_verify(request: Request):
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    if not client_id:
        return JSONResponse({"error": "GOOGLE_OAUTH_CLIENT_ID not configured"}, status_code=503)

    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "malformed JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    credential = body.get("credential")
    if not credential:
        return JSONResponse({"error": "missing credential"}, status_code=400)

    try:
        identity = verify_credential(credential, client_id)
    except InvalidGoogleToken as e:
        return JSONResponse({"error": f"invalid Google credential: {e}"}, status_code=401)

    token = auth_store.get_or_create_token(identity["sub"], identity["email"])
    return JSONResponse({"mcp_token": token, "email": identity["email"]})


if __name__ == "__main__":
    import secrets

    import uvicorn

    from metrics.store_sqlite import DB_PATH as _metrics_db_path

    _maybe_seed_demo_db(os.environ.get("DEMO_SEED_SRC"), _metrics_db_path)

    # No token configured → generate one for this run and print it,
    # rather than starting the /mcp endpoint wide open. Set MCP_AUTH_TOKEN
    # yourself for a stable value across restarts (e.g. so a browser tab
    # left open doesn't need re-pasting every time you restart the server).
    # This is the OWNER's token — friends should sign in via /auth/login
    # instead of being handed this one, so revoking a single friend's
    # access doesn't mean rotating everyone's token.
    mcp_auth_token = _resolve_owner_token(os.environ.get("MCP_AUTH_TOKEN"))
    if not mcp_auth_token:
        mcp_auth_token = secrets.token_urlsafe(24)
        print(
            "\n─── No MCP_AUTH_TOKEN set — generated one for this run "
            + "─" * 10
            + f"\n\n    {mcp_auth_token}\n\n"
            "This is YOUR (owner) token — paste it into any MCP-config-based "
            "client (Claude Desktop, VS Code, curl) to authenticate. The chat "
            "UI's own MCP panel uses Google sign-in instead, not this token.\n"
        )

    server_port = int(os.environ.get("MCP_SERVER_PORT", "8787"))
    if os.environ.get("GOOGLE_OAUTH_CLIENT_ID"):
        # Google Identity Services only reliably honors "localhost" as an
        # authorized origin for plain-HTTP local development — 127.0.0.1,
        # though it resolves to the same loopback server, is a different
        # string, and GSI's runtime origin check can reject it live even
        # when the Cloud Console UI accepts saving it as an authorized
        # JavaScript origin. Console setup (README) uses localhost too.
        print(f"Google sign-in enabled — friends can get their own token at http://localhost:{server_port}/auth/login\n")
    else:
        print("GOOGLE_OAUTH_CLIENT_ID not set — /auth/login will report sign-in as unavailable; only the owner token above works.\n")

    # Built with server.run(transport="streamable-http") — but that path
    # doesn't expose the underlying Starlette app for CORS configuration,
    # and the chat panel's real MCP handshake (web/mcp-client.js, running
    # from web.server's own origin/port) needs cross-origin access to
    # /mcp. Constructing the app via streamable_http_app() ourselves and
    # wrapping it in CORSMiddleware is the same server, just runnable
    # with the middleware attached.
    web_port = int(os.environ.get("WEB_SERVER_PORT", "8788"))
    allowed_origins = [f"http://127.0.0.1:{web_port}", f"http://localhost:{web_port}"]
    # Comma-separated list of the deployed chat UI's real origin(s) — e.g.
    # both a Cloud Run URL and a Cloudflare Worker reverse-proxy in front
    # of it (see cloudflare-proxy/), since a browser sees these as
    # different origins even though they ultimately reach the same
    # service. Local dev origins above stay allowed regardless.
    deployed_chat_origins = os.environ.get("CHAT_UI_ORIGIN", "")
    allowed_origins.extend(origin.strip() for origin in deployed_chat_origins.split(",") if origin.strip())
    http_app = server.streamable_http_app()
    # Auth middleware added before CORS so CORS ends up outermost (Starlette
    # runs the most-recently-added middleware first) — CORS preflight
    # (OPTIONS, no Authorization header) gets handled and answered before
    # ever reaching the auth check, and CORS headers still land on 401s.
    http_app.add_middleware(MultiTokenAuthMiddleware, owner_token=mcp_auth_token)
    http_app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type", "Mcp-Session-Id", "Accept", "Authorization"],
        expose_headers=["Mcp-Session-Id"],
    )
    # HOST defaults to loopback-only for local dev; Cloud Run sets PORT
    # itself and requires binding 0.0.0.0, so HOST=0.0.0.0 is set in the
    # container's env there. Port overridable so tests can boot a
    # throwaway instance without colliding with a real dev.server session
    # already running on the default port.
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", server_port))
    uvicorn.run(http_app, host=host, port=port)
