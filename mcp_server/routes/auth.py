"""Google sign-in, the connect page, and the live dashboard SPA — the
human-facing side of this server. auth_login builds and serves the
sign-in/connect/dashboard HTML+JS as large inline templates (kept as-is
in one function rather than decomposed further: it's one cohesive page
with heavy string interpolation, and splitting the template strings out
from the route handler risks subtly breaking escaped JS). auth_verify
completes Google sign-in and mints this user's MCP token."""

import os

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from mcp_server.auth import store as auth_store
from mcp_server.app import server
from mcp_server.auth.google import InvalidGoogleToken, verify_credential


# Google sign-in: the pre-auth flow that mints a per-user MCP token; not
# gated by MultiTokenAuthMiddleware, which only checks /mcp, /api/, and
# /otlp.


_PAGE_STYLE = """
  :root {
    --bg: #0a0d13; --bg-raised: #12161f; --bg-raised-2: #161b26; --bg-sunken: #060a0f;
    --border: #232a38; --border-soft: #1b212d;
    --text: #e4eaf3; --text-dim: #8b96a8; --text-dimmer: #5f6b7d;
    --accent: #35e0c8; --accent-2: #6c8dff; --accent-dim: #163832;
    --warn: #f5b955; --warn-dim: #3a2c14; --warn-border: #4a3a1a;
    --ok: #4ade80; --ok-dim: #13301f;
    --err: #f2657a; --err-dim: #3a1620;
    --thinking: #c084fc; --thinking-dim: #2c1c42;
    --cat-system: #6b7280; --cat-tools: #8b98ac; --cat-user: #e4eaf3;
    --cat-reasoning: #35e0c8; --cat-thinking: #c084fc; --cat-toolcall: #f5b955;
    --cat-toolresult: #4ade80; --cat-answer: #6c8dff;
    --shadow: 0 1px 2px rgba(0,0,0,0.3), 0 12px 32px -12px rgba(0,0,0,0.55);
    --radius: 16px; --radius-sm: 10px;
  }
  * { box-sizing: border-box; }
  html { background: var(--bg); }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", sans-serif;
    min-height: 100vh;
    background:
      radial-gradient(1200px 480px at 50% -10%, rgba(53,224,200,0.10), transparent 60%),
      var(--bg);
    color: var(--text); line-height: 1.6; -webkit-font-smoothing: antialiased;
  }
  /* Sign-in / connect-config content stays a narrow reading column;
     the post-auth dashboard screen (#dashboard-screen below) is a
     separate full-width sibling, not nested inside this. */
  .narrow-page { max-width: 640px; margin: 0 auto; padding: 4.5rem 1.5rem 5rem; }
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
  .otel-optin { margin-top: 0.9rem; padding: 0.8rem; border: 1px solid var(--warn-border); background: var(--warn-dim); border-radius: 8px; }

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

  /* Live dashboard — full session-list + tabbed session-detail rebuild,
     matching the approved mockup (see PR description). Shown to every
     authenticated user for their own data right after Authorize.
     dash-empty/dash-error are the two loading/error states, reused
     across the KPI strip, session list, and detail panel. */
  .dash-empty, .dash-error { color: var(--text-dim); font-size: 0.85rem; padding: 0.6rem 0.85rem; }
  .dash-error { color: var(--warn); }

  .mono { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-variant-numeric: tabular-nums; }
  #dashboard-screen { background: var(--bg); }
  .topbar {
    display: flex; align-items: center; gap: 1rem; padding: 0.85rem 1.5rem;
    border-bottom: 1px solid var(--border); background: var(--bg-raised); position: sticky; top: 0; z-index: 5;
  }
  .topbar .brand { margin-bottom: 0; }
  .topbar-spacer { flex: 1; }
  .live-pill {
    display: inline-flex; align-items: center; gap: 0.4rem; font-size: 11.5px; color: var(--ok);
    background: var(--ok-dim); border: 1px solid color-mix(in srgb, var(--ok) 35%, transparent);
    padding: 0.28rem 0.65rem; border-radius: 999px; font-weight: 550;
  }
  .live-dot { width: 6px; height: 6px; border-radius: 999px; background: var(--ok); animation: pulse 2s infinite; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
  @media (prefers-reduced-motion: reduce) { .live-dot { animation: none; } }
  .identity-menu { position: relative; }
  .identity-trigger {
    display: flex; align-items: center; gap: 0.5rem; padding: 0.35rem 0.6rem 0.35rem 0.4rem;
    border: 1px solid var(--border); border-radius: 999px; background: var(--bg-raised-2); color: var(--text-dim);
    font-size: 0.87rem; cursor: pointer; transition: border-color 0.15s ease, color 0.15s ease;
  }
  .identity-trigger:hover, .identity-trigger[aria-expanded="true"] { border-color: var(--accent); color: var(--text); }
  .identity-trigger .chev { font-size: 0.65rem; color: var(--text-dimmer); transition: transform 0.15s ease; }
  .identity-trigger[aria-expanded="true"] .chev { transform: rotate(180deg); }
  .identity-dropdown {
    position: absolute; top: calc(100% + 0.5rem); right: 0; min-width: 12rem; z-index: 20;
    border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg-raised);
    box-shadow: var(--shadow); overflow: hidden; padding: 0.35rem;
  }
  .identity-dropdown-email {
    padding: 0.5rem 0.65rem 0.6rem; font-size: 0.78rem; color: var(--text-dimmer);
    border-bottom: 1px solid var(--border-soft); margin-bottom: 0.35rem; word-break: break-all;
  }
  .identity-dropdown button {
    display: flex; align-items: center; gap: 0.55rem; width: 100%; text-align: left; font-size: 0.87rem;
    padding: 0.5rem 0.65rem; border: none; background: none; color: var(--text); border-radius: 7px; cursor: pointer;
  }
  .identity-dropdown button:hover { background: var(--bg-raised-2); }
  .identity-dropdown button.danger { color: var(--err); }
  .identity-dropdown button.danger:hover { background: var(--err-dim); }
  .layout { display: grid; grid-template-columns: 1fr; gap: 1rem; padding: 1.4rem 1.5rem 4rem; max-width: 1320px; margin: 0 auto; }
  .kpi-strip { display: grid; grid-template-columns: repeat(6, 1fr); gap: 0.7rem; }
  @media (max-width: 1100px) { .kpi-strip { grid-template-columns: repeat(3, 1fr); } }
  .kpi { background: var(--bg-raised); border: 1px solid var(--border); border-radius: var(--radius); padding: 0.85rem 1rem; box-shadow: var(--shadow); display: flex; flex-direction: column; gap: 0.35rem; min-width: 0; }
  .kpi-label { font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-dimmer); font-weight: 650; }
  .kpi-value { font-size: 20px; font-weight: 650; letter-spacing: -0.01em; color: var(--text); }
  .kpi-value small { font-size: 12px; color: var(--text-dimmer); font-weight: 500; }
  .kpi-value.accent-warn { color: var(--warn); }
  .kpi-delta { font-size: 11px; font-weight: 550; }
  .kpi-delta.up { color: var(--ok); } .kpi-delta.down { color: var(--err); } .kpi-delta.flat { color: var(--text-dimmer); }
  .body-grid { display: grid; grid-template-columns: 340px 1fr; gap: 1rem; align-items: start; }
  @media (max-width: 1000px) { .body-grid { grid-template-columns: 1fr; } }
  .panel { background: var(--bg-raised); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: var(--shadow); overflow: hidden; }
  .panel-head { display: flex; align-items: center; justify-content: space-between; gap: 0.6rem; padding: 0.85rem 1rem; border-bottom: 1px solid var(--border-soft); }
  .panel-title { font-size: 13px; font-weight: 650; color: var(--text); }
  .panel-body { padding: 0.6rem; }
  .filter-row { display: flex; gap: 0.35rem; padding: 0.6rem 0.85rem 0; flex-wrap: wrap; }
  .chip { font-size: 11px; font-weight: 550; padding: 0.28rem 0.6rem; border-radius: 999px; border: 1px solid var(--border); background: var(--bg-raised-2); color: var(--text-dim); cursor: pointer; }
  .chip.active { background: var(--accent-dim); color: var(--accent); border-color: color-mix(in srgb, var(--accent) 40%, transparent); }
  .session-list { display: flex; flex-direction: column; gap: 0.4rem; padding: 0.7rem; max-height: 640px; overflow-y: auto; }
  .session-row { border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 0.65rem 0.75rem; cursor: pointer; background: var(--bg-raised); transition: border-color .12s ease, background .12s ease; }
  .session-row:hover { border-color: var(--text-dimmer); }
  .session-row.active { border-color: var(--accent); background: var(--accent-dim); }
  .session-row-top { display: flex; align-items: center; gap: 0.45rem; margin-bottom: 0.3rem; }
  .src-badge { font-size: 9.5px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.03em; padding: 0.12rem 0.4rem; border-radius: 5px; flex-shrink: 0; }
  .src-badge.cc { background: var(--thinking-dim); color: var(--thinking); }
  .src-badge.gh { background: var(--accent-dim); color: var(--accent); }
  .src-badge.bd { background: var(--warn-dim); color: var(--warn); }
  .session-prompt { font-size: 12px; font-weight: 550; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text); }
  .session-row-meta { display: flex; align-items: center; justify-content: space-between; font-size: 10.5px; color: var(--text-dimmer); }
  .ctx-badge { display: inline-flex; align-items: center; gap: 0.25rem; font-weight: 650; }
  .ctx-badge.warn-level { color: var(--warn); }
  .ctx-badge.err-level { color: var(--err); }
  .tabs { display: flex; gap: 0.2rem; padding: 0 1rem; border-bottom: 1px solid var(--border-soft); }
  .tab { padding: 0.7rem 0.15rem; margin-right: 1.2rem; font-size: 12.5px; font-weight: 600; color: var(--text-dimmer); border-bottom: 2px solid transparent; cursor: pointer; background: none; border-left: none; border-right: none; border-top: none; }
  .tab.active { color: var(--text); border-bottom-color: var(--accent); }
  .tab-content { display: none; }
  .tab-content.active { display: block; }
  .tool-table { display: flex; flex-direction: column; padding: 0 1.1rem 1.1rem; overflow-x: auto; }
  .tool-row { display: grid; grid-template-columns: 1.6rem 6.5rem 4.2rem 3.6rem 1fr 5rem; gap: 0.6rem; align-items: center; padding: 0.5rem 0.3rem; border-bottom: 1px solid var(--border-soft); font-size: 11.5px; min-width: 560px; }
  .tool-row:last-child { border-bottom: none; }
  .tool-row.tool-head { color: var(--text-dimmer); font-size: 10px; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 700; }
  .tool-row .args { color: var(--text-dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .detail-head { padding: 1rem 1.1rem 0.2rem; }
  .detail-title { font-size: 15px; font-weight: 650; margin-bottom: 0.3rem; color: var(--text); }
  .detail-meta { display: flex; flex-wrap: wrap; gap: 0.9rem; font-size: 11.5px; color: var(--text-dim); margin-bottom: 0.9rem; }
  .metric-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(118px, 1fr)); gap: 0.6rem; padding: 0 1.1rem 1rem; }
  .metric-tile { border: 1px solid var(--border-soft); border-radius: var(--radius-sm); padding: 0.6rem 0.7rem; background: var(--bg-raised-2); }
  .metric-tile .m-label { font-size: 10px; color: var(--text-dimmer); text-transform: uppercase; letter-spacing: 0.05em; font-weight: 650; }
  .metric-tile .m-value { font-size: 16px; font-weight: 650; margin-top: 0.15rem; color: var(--text); }
  .metric-tile .m-sub { font-size: 10.5px; color: var(--text-dimmer); margin-top: 0.1rem; }
  .metric-tile.accent-ok .m-value { color: var(--ok); }
  .metric-tile.accent-warn .m-value { color: var(--warn); }
  .section-heading { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.055em; color: var(--text-dimmer); padding: 0 1.1rem; margin: 0.4rem 0 0.55rem; }
  .ctx-bar { display: flex; height: 10px; width: calc(100% - 2.2rem); margin: 0.3rem 1.1rem 0; border-radius: 999px; overflow: hidden; border: 1px solid var(--border-soft); }
  .ctx-bar > div { height: 100%; }
  .ctx-legend { display: flex; flex-wrap: wrap; gap: 0.75rem; font-size: 10.5px; color: var(--text-dim); padding: 0.75rem 1.1rem 0.9rem; }
  .ctx-legend span { display: inline-flex; align-items: center; gap: 0.32rem; }
  .ctx-legend i { width: 7px; height: 7px; border-radius: 999px; display: inline-block; }
  .block-list { display: flex; flex-direction: column; gap: 0.25rem; padding: 0 0.6rem 0.9rem; }
  .block-row { display: flex; align-items: center; gap: 0.55rem; padding: 0.45rem 0.55rem; border-radius: 8px; font-size: 12px; cursor: pointer; }
  .block-row:hover { background: var(--bg-raised-2); }
  .block-dot { width: 7px; height: 7px; border-radius: 999px; flex-shrink: 0; }
  .block-label { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text); }
  .block-label .redacted { color: var(--text-dimmer); font-style: italic; }
  .block-tok { color: var(--text-dimmer); font-size: 11px; flex-shrink: 0; }
  .block-pct { font-size: 10.5px; color: var(--text-dimmer); width: 3.4rem; text-align: right; flex-shrink: 0; }
  .block-chev { color: var(--text-dimmer); font-size: 10px; flex-shrink: 0; width: 0.9rem; text-align: center; transition: transform 0.15s ease; }
  .block-row.expanded .block-chev { transform: rotate(90deg); }
  .block-detail {
    margin: 0 0.55rem 0.4rem; padding: 0.7rem 0.85rem; border-radius: var(--radius-sm);
    background: var(--bg-sunken); border: 1px solid var(--border-soft);
    font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 11.5px; line-height: 1.55;
    color: var(--text-dim); white-space: pre-wrap; word-break: break-word; max-height: 20rem; overflow-y: auto;
  }
  .block-detail.unavailable { font-family: inherit; font-style: italic; color: var(--text-dimmer); white-space: normal; }
  .two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 0.8rem; padding: 0 1.1rem 1.1rem; }
  @media (max-width: 760px) { .two-col { grid-template-columns: 1fr; } }
  .subpanel { border: 1px solid var(--border-soft); border-radius: var(--radius-sm); background: var(--bg-raised-2); padding: 0.75rem 0.85rem; }
  .subpanel h4 { font-size: 11.5px; font-weight: 650; margin-bottom: 0.6rem; color: var(--text); }
  .bar-row { display: flex; align-items: center; gap: 0.5rem; font-size: 11px; margin-bottom: 0.45rem; }
  .bar-row .b-name { width: 108px; flex-shrink: 0; color: var(--text-dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .bar-track { flex: 1; height: 7px; background: var(--bg-sunken); border-radius: 999px; overflow: hidden; }
  .bar-fill { height: 100%; border-radius: 999px; background: var(--accent); }
  .bar-fill.err { background: var(--err); }
  .b-val { width: 3.8rem; text-align: right; color: var(--text-dimmer); flex-shrink: 0; }
  .kv-list { display: flex; flex-direction: column; }
  .kv-line { display: flex; justify-content: space-between; padding: 0.4rem 0; border-bottom: 1px solid var(--border-soft); font-size: 11.5px; }
  .kv-line .k { color: var(--text-dim); } .kv-line .v { color: var(--text); font-weight: 550; }
  .status-dot { width: 7px; height: 7px; border-radius: 999px; display: inline-block; margin-right: 0.35rem; }
  .status-dot.ok { background: var(--ok); } .status-dot.err { background: var(--err); } .status-dot.warn { background: var(--warn); }
  .badge-pill { font-size: 10px; font-weight: 650; padding: 0.15rem 0.5rem; border-radius: 999px; display: inline-flex; align-items: center; gap: 0.3rem; }
  .badge-pill.ok { background: var(--ok-dim); color: var(--ok); }
  .badge-pill.warn { background: var(--warn-dim); color: var(--warn); }
  .badge-pill.err { background: var(--err-dim); color: var(--err); }
  .badge-pill.dim { background: var(--bg-sunken); color: var(--text-dimmer); }
  .settings-wrap { max-width: 880px; margin: 0 auto; padding: 0; display: flex; flex-direction: column; gap: 1rem; }
  .settings-head p { color: var(--text-dim); font-size: 12.5px; margin: 0.3rem 0 0; max-width: 46rem; }
  .config-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 1.2rem; padding: 0.95rem 0; border-bottom: 1px solid var(--border-soft); }
  .config-row:last-child { border-bottom: none; }
  .config-copy { max-width: 30rem; }
  .config-copy .c-title { font-size: 13px; font-weight: 600; margin-bottom: 0.2rem; color: var(--text); }
  .config-copy .c-desc { font-size: 11.5px; color: var(--text-dim); line-height: 1.55; }
  .config-control { flex-shrink: 0; display: flex; align-items: center; gap: 0.6rem; padding-top: 0.15rem; }
  .switch { position: relative; width: 2.3rem; height: 1.3rem; flex-shrink: 0; }
  .switch input { opacity: 0; width: 0; height: 0; }
  .switch-track { position: absolute; inset: 0; background: var(--bg-sunken); border: 1px solid var(--border); border-radius: 999px; cursor: pointer; transition: background .15s ease; }
  .switch-track::before { content: ""; position: absolute; width: 0.95rem; height: 0.95rem; border-radius: 999px; background: var(--text-dimmer); top: 50%; left: 0.16rem; transform: translateY(-50%); transition: transform .15s ease, background .15s ease; }
  .switch input:checked + .switch-track { background: var(--accent-dim); border-color: color-mix(in srgb, var(--accent) 45%, transparent); }
  .switch input:checked + .switch-track::before { transform: translate(1rem, -50%); background: var(--accent); }
  select.cfg-select { background: var(--bg-raised); border: 1px solid var(--border); color: var(--text); border-radius: 8px; padding: 0.35rem 0.6rem; font-size: 12px; font-family: inherit; }
  input.cfg-input { background: var(--bg-raised); border: 1px solid var(--border); color: var(--text); border-radius: 8px; padding: 0.35rem 0.6rem; font-size: 12px; font-family: inherit; width: 5.5rem; }
  .tag-input-row { display: flex; gap: 0.4rem; flex-wrap: wrap; }
  .tag { font-size: 11px; padding: 0.22rem 0.55rem; border-radius: 999px; background: var(--bg-sunken); color: var(--text-dim); border: 1px solid var(--border); }
  .disclosure-note { display: flex; gap: 0.6rem; align-items: flex-start; padding: 0.85rem 1rem; border-radius: var(--radius-sm); background: var(--warn-dim); border: 1px solid var(--warn-border); font-size: 11.5px; color: var(--text); }
  .disclosure-note strong { color: var(--warn); }
  .range-row { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.2rem; }
  .quota-strip { display: grid; grid-template-columns: 1fr 1fr; gap: 0.7rem; }
  .quota-card { background: var(--bg-raised); border: 1px solid var(--border); border-radius: var(--radius); padding: 0.85rem 1rem; box-shadow: var(--shadow); position: relative; }
  .quota-card.pending::after { content: "pending data source"; position: absolute; top: 0.7rem; right: 0.8rem; font-size: 9px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; color: var(--text-dimmer); background: var(--bg-sunken); border: 1px dashed var(--border); padding: 0.15rem 0.4rem; border-radius: 5px; }
  .quota-top { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 0.5rem; }
  .quota-top .q-label { font-size: 11px; font-weight: 650; color: var(--text-dim); }
  .quota-top .q-pct { font-size: 17px; font-weight: 650; color: var(--text); }
  .quota-track { height: 8px; border-radius: 999px; background: var(--bg-sunken); overflow: hidden; margin-bottom: 0.4rem; }
  .quota-fill { height: 100%; border-radius: 999px; background: var(--accent); }
  .quota-fill.hot { background: var(--warn); }
  .quota-sub { font-size: 10.5px; color: var(--text-dimmer); }
  .insight-strip { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 0.7rem; }
  .insight-card { background: var(--bg-raised); border: 1px solid var(--border); border-radius: var(--radius); padding: 0.85rem 1rem; box-shadow: var(--shadow); }
  .insight-card .i-label { font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-dimmer); font-weight: 650; margin-bottom: 0.35rem; }
  .insight-card .i-body { font-size: 12.5px; color: var(--text); line-height: 1.5; }
  .insight-card .i-body strong { color: var(--accent); font-weight: 650; }
  .ctx-dot { width: 7px; height: 7px; border-radius: 999px; display: inline-block; flex-shrink: 0; background: var(--text-dimmer); }
  .ctx-dot.warn { background: var(--warn); }
  .ctx-dot.err { background: var(--err); }
  .agent-tabs { display: flex; gap: 0.4rem; padding: 0.9rem 1.1rem 0.9rem; flex-wrap: wrap; }
  .agent-tab { display: flex; align-items: center; gap: 0.4rem; font-size: 11px; font-weight: 600; padding: 0.32rem 0.7rem; border-radius: 999px; border: 1px solid var(--border); background: var(--bg-raised); color: var(--text-dim); cursor: default; }
  .agent-tab.active { background: var(--thinking-dim); color: var(--thinking); border-color: color-mix(in srgb, var(--thinking) 40%, transparent); }
  .agent-tab .a-tok { font-family: ui-monospace, "SF Mono", Menlo, monospace; color: var(--text-dimmer); font-weight: 500; }
  .icon-btn { font-size: 0.8rem; font-weight: 500; padding: 0.4rem 0.75rem; border-radius: 8px; border: 1px solid var(--border); background: var(--bg-raised-2); color: var(--text-dim); cursor: pointer; }
  .icon-btn:hover { border-color: var(--accent); color: var(--accent); background: var(--accent-dim); }
"""


@server.custom_route("/auth/login", methods=["GET"])
async def auth_login(request: Request):
    intro = """
<div class="hero">
  <span class="hero-eyebrow">Model Context Protocol server</span>
  <h1>mcp-context-inspector</h1>
  <p class="sub">Execution metrics and a full Context Window Explorer for Bedrock-based agents and Claude Code — over a real MCP server, not a mock.</p>
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
  <h3>What this gives your agent</h3>
  <ul class="features">
    <li>Real per-session cost, token, and tool-call metrics — 7 read-only MCP tools</li>
    <li>The <strong>Context Window Explorer</strong> — exactly what entered the model's context window, block by block, with honest token estimates</li>
    <li>Your own data, isolated from anyone else connected to this server — sign in below and everything you record or query is scoped to your account</li>
  </ul>
</div>
<details>
  <summary>What is an MCP server?</summary>
  <p>MCP (Model Context Protocol) is an open standard that lets an LLM or agent call tools over a
  normal HTTP connection. This server exposes read/write tools for agent execution
  data, and is built to support two integration paths: Bedrock-based agents calling the
  <code>record_session</code> tool directly, and Claude Code.</p>
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
<div class="narrow-page">
<div id="intro">{intro}
<div class="card">
  <h3>Sign in to get your token</h3>
  <p style="margin-top:0; color: var(--text-dim); font-size: 0.9rem;">One click — no password, no account to create here.</p>
  <div id="g_id_onload" data-client_id="{client_id}" data-callback="onSignIn"></div>
  <div class="g_id_signin" data-type="standard" data-theme="filled_black"></div>
</div>
</div>
<div id="landing" class="hidden"></div>
</div>
<div id="dashboard-screen" class="hidden"></div>
<script>
  const mcpUrl = window.location.origin + "/mcp";

  function connectPage(email, token) {{
    const isLocalHost = ["localhost", "127.0.0.1"].includes(window.location.hostname);
    const claudeConfig = JSON.stringify({{
      mcpServers: {{
        "context-inspector": {{
          url: mcpUrl,
          headers: {{ Authorization: "Bearer " + token }}
        }}
      }}
    }}, null, 2);
    const rawHeader = "Authorization: Bearer " + token;
    const curlCmd = 'curl -H "Authorization: Bearer ' + token + '" ' + window.location.origin + '/api/sessions';
    const otlpUrl = window.location.origin + "/otlp";
    const claudeOtelSnippet = [
      "export CLAUDE_CODE_ENABLE_TELEMETRY=1",
      "export OTEL_LOGS_EXPORTER=otlp",
      "export OTEL_METRICS_EXPORTER=otlp",
      "export OTEL_EXPORTER_OTLP_PROTOCOL=http/json",
      "export OTEL_EXPORTER_OTLP_ENDPOINT=" + otlpUrl,
      'export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer ' + token + '"',
    ].join("\\n");
    const claudeOtelOptin = [
      "export OTEL_LOG_RAW_API_BODIES=1",
      // Claude Code truncates any content-bearing attribute (including this
      // raw body) at 60KB by default — real sessions with a system prompt
      // and tool specs exceed that almost immediately, which truncates the
      // body's JSON mid-string and makes it unparseable, silently losing
      // that turn's Context Explorer detail (confirmed via a live capture).
      // Raised here to 1MB, comfortably inside this server's own 25MB cap
      // on a whole OTLP batch.
      "export CLAUDE_CODE_OTEL_CONTENT_MAX_LENGTH=1048576",
    ].join("\\n");
    const copilotOtelSnippet = [
      "export COPILOT_OTEL_ENABLED=true",
      "export OTEL_EXPORTER_OTLP_ENDPOINT=" + otlpUrl,
      'export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer ' + token + '"',
    ].join("\\n");
    const copilotOtelOptin = "export COPILOT_OTEL_CAPTURE_CONTENT=true";

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
          <button class="copy" onclick="signOut()">Sign out</button>
        </div>
        <span id="token-raw" class="hidden">` + token + `</span>
        <span id="url-raw" class="hidden">` + mcpUrl + `</span>
      </div>

      ` + (isLocalHost ? `
      <div id="local-setup-card" class="card accent">
        <h3>Set up Claude Code automatically</h3>
        <p class="card-hint">Writes the MCP connection and telemetry config below directly into
        your own <code>~/.claude/settings.json</code> — the same file the manual snippets below
        would have you paste into by hand, applied for you instead. Your existing settings are
        backed up first and merged, never overwritten.</p>
        <p class="card-hint" style="color: var(--ok);">This is a local file write on your own
        machine only — nothing here is sent anywhere except this server, which is also running
        on your machine right now.</p>
        <button class="copy" onclick="applyLocalConfig()" id="local-setup-btn">Apply to my Claude Code config</button>
        <div id="local-setup-result" style="margin-top: 0.7rem; font-size: 0.85rem;"></div>
      </div>
      ` : `
      <div id="local-script-card" class="card accent">
        <h3>Download setup script</h3>
        <p class="card-hint">Gets you connected everywhere — MCP query tools <strong>and</strong>
        automatic telemetry — with one script you run once, locally. This server is deployed, so
        it can't write to your machine directly; the script does the exact same
        <code>~/.claude/settings.json</code> merge <a href="#advanced-setup" onclick="showConnectTab('claude'); const d = document.querySelector('details'); d.open = true; d.scrollIntoView({{behavior:'smooth'}}); return false;" style="color:inherit; text-decoration:underline;">the manual snippets below</a>
        would have you paste in by hand, but running locally, on your own machine, under your
        own inspection. Your existing settings are backed up first and merged, never overwritten.</p>
        <button class="copy" onclick="downloadLocalScript()" id="local-script-btn">Download setup script</button>
        <div id="local-script-result" style="margin-top: 0.7rem; font-size: 0.85rem;"></div>
        <p class="card-hint" style="margin-top: 0.6rem;">
          Prefer not to run a script? <a href="https://claude.ai/new#settings/customize-connectors" target="_blank" rel="noopener" onclick="document.querySelector('details').open=true;">Connect via claude.ai Connectors instead &rarr;</a>
          — opens claude.ai's Connectors settings in a new tab. Paste just the MCP server URL from
          "Your connection" above (no token needed — you'll sign in with Google right there) under
          <strong>Add custom connector</strong>. Full steps in the "Advanced" section below, now expanded.
        </p>
      </div>
      `) + `

      <details>
        <summary>Advanced: manual setup, or connecting a different client</summary>
        <div style="padding-left: 1.7rem;">
        ` + (isLocalHost ? `` : `
        <div id="connectors-info" style="margin-bottom: 1.1rem;">
          <p class="card-hint"><strong>claude.ai Connectors</strong> — this server can't write to your local
          Claude Code config from here; it can only do that for itself when it's the one running on your
          machine (self-hosted at <code>localhost</code>). But claude.ai's own Connectors feature gets you MCP
          query access (ask "what did session X cost") in every session, everywhere, with zero local files
          touched — it just can't carry the OTLP env vars that power automatic telemetry, so the dashboard
          won't auto-populate as you code unless you also paste the "Claude Code (live telemetry)" snippet
          below once per machine.</p>
          <ol class="card-hint" style="padding-left: 1.2rem; margin: 0.7rem 0;">
            <li>Copy the MCP server URL above.</li>
            <li>Go to <strong>claude.ai &rarr; Customize &rarr; Connectors &rarr; Add custom connector.</strong></li>
            <li>Paste the URL and click <strong>Add</strong> — leave the OAuth Client ID/Secret fields blank,
            those aren't used here. claude.ai will open a Google sign-in page for this server automatically;
            once you sign in, the connector is live. No token to copy or paste anywhere.</li>
          </ol>
          <a class="copy" href="https://claude.ai/new#settings/customize-connectors" target="_blank" rel="noopener" style="display:inline-block; text-decoration:none;">Open claude.ai Connectors</a>
        </div>
        `) + `
        <div class="tab-row" style="margin-top: 0.9rem;">
          <button class="tab-btn active" data-tab="claude" onclick="showConnectTab('claude')">Claude Code</button>
          <button class="tab-btn" data-tab="api" onclick="showConnectTab('api')">API / curl</button>
          <button class="tab-btn" data-tab="claude-otel" onclick="showConnectTab('claude-otel')">Claude Code (live telemetry)</button>
          <button class="tab-btn" data-tab="copilot-otel" onclick="showConnectTab('copilot-otel')">Copilot (live telemetry)</button>
        </div>

        <div class="tab-panel active" data-panel="claude">
          <p class="card-hint">Add this to your client's MCP server config:</p>
          <pre id="claude-config">` + claudeConfig + `</pre>
          <button class="copy" onclick="copyText('claude-config')">Copy config</button>
        </div>
        <div class="tab-panel" data-panel="api">
          <p class="card-hint">Bedrock-based agents — point them at the MCP server URL above with this header on every request:</p>
          <pre id="raw-header">` + rawHeader + `</pre>
          <button class="copy" onclick="copyText('raw-header')">Copy header</button>
          <p class="card-hint" style="margin-top: 0.9rem;">curl (debugging):</p>
          <pre id="curl-cmd">` + curlCmd + `</pre>
          <button class="copy" onclick="copyText('curl-cmd')">Copy curl</button>
        </div>
        <div class="tab-panel" data-panel="claude-otel">
          <p class="card-hint">Claude Code exports its own OpenTelemetry data natively — point it at this server instead of
          (or alongside) the MCP connection to get live token/cost/tool-call telemetry with no extra tool calls needed:</p>
          <pre id="claude-otel-snippet">` + claudeOtelSnippet + `</pre>
          <button class="copy" onclick="copyText('claude-otel-snippet')">Copy snippet</button>
          <div class="otel-optin">
            <p class="card-hint"><strong>Optional — powers the per-session Context Window Explorer.</strong> Without this,
            you still get token counts, cost, and tool-call telemetry from the snippet above. With it, Claude Code's own
            raw request/response bodies are captured, giving you the full block-by-block context breakdown — but per
            Claude Code's own docs, this is a materially bigger disclosure: "bodies include the entire conversation
            history." Add it only if you want that level of detail:</p>
            <pre id="claude-otel-optin">` + claudeOtelOptin + `</pre>
            <button class="copy" onclick="copyText('claude-otel-optin')">Copy opt-in line</button>
          </div>
        </div>
        <div class="tab-panel" data-panel="copilot-otel">
          <p class="card-hint">GitHub Copilot (VS Code) also exports OpenTelemetry natively — this covers Copilot Chat,
          which is VS Code's native AI surface, so no separate VS Code integration is needed:</p>
          <pre id="copilot-otel-snippet">` + copilotOtelSnippet + `</pre>
          <button class="copy" onclick="copyText('copilot-otel-snippet')">Copy snippet</button>
          <div class="otel-optin">
            <p class="card-hint"><strong>Optional — powers the per-session Context Window Explorer.</strong> Without this,
            you still get token counts and tool-call telemetry from the snippet above. With it, Copilot exposes its own
            structured prompt/response content (` + "`gen_ai.input.messages`/`gen_ai.output.messages`" + `) for the full
            context breakdown — a bigger disclosure than token counts alone. Add it only if you want that level of detail:</p>
            <pre id="copilot-otel-optin">` + copilotOtelOptin + `</pre>
            <button class="copy" onclick="copyText('copilot-otel-optin')">Copy opt-in line</button>
          </div>
        </div>
        </div>
      </details>

      <div class="card accent">
        <h3>Live dashboard <span class="badge">ready</span></h3>
        <p class="card-hint">Your own sessions only — every ` + "`record_session`" + ` call from your LLM/agent
        (recorded through the token above) shows up here within a few seconds, including the full
        Context Window Explorer breakdown. No separate app needed.</p>
        <button class="copy" onclick="goToDashboard()">Proceed to dashboard &rarr;</button>
      </div>

      <details>
        <summary>How do I record my own agent's sessions here, not just read?</summary>
        <p>Call the <code>record_session</code> MCP tool (or POST <code>/api/record-session</code>) with the same
        bearer token — whatever you record is automatically attributed to you, the same way reads are scoped.
        See the package README's Auth section for the exact request shape.</p>
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

  // --- Post-auth dashboard screen -------------------------------------
  // A separate full-width screen (not nested in the narrow sign-in/
  // config column) reached via "Proceed to dashboard" after a fresh
  // sign-in, or automatically on a returning visit with a stored token
  // (see rehydrateFromStorage) — matches the approved dashboard mockup's
  // own topbar + wide layout rather than squeezing it into a card.
  function avatarInitial(email) {{
    return (email || "?").trim()[0]?.toUpperCase() || "?";
  }}

  function dashboardScreen(email, avatarLetter) {{
    return `
      <div class="topbar">
        <div class="brand">
          <span class="brand-mark">&#9670;</span>
          <span style="font-weight:650; font-size:0.95rem;">mcp-context-inspector</span>
        </div>
        <div class="topbar-spacer"></div>
        <span class="live-pill"><span class="live-dot"></span> live</span>
        <button class="icon-btn" onclick="toggleSettings()">&#9881; Project settings</button>
        <div class="identity-menu">
          <button class="identity-trigger" aria-expanded="false" onclick="toggleIdentityMenu(event)">
            <span class="avatar">` + avatarLetter + `</span>
            <span class="chev">&#9662;</span>
          </button>
          <div class="identity-dropdown hidden" id="identity-dropdown">
            <div class="identity-dropdown-email">` + email + `</div>
            <button onclick="closeIdentityMenu(); backToConnect();">&larr; Token &amp; config</button>
            <button onclick="closeIdentityMenu(); copyCurrentToken();">&#9112; Copy token</button>
            <button onclick="closeIdentityMenu(); refreshDashboard(currentToken);">&#8635; Refresh now</button>
            <button class="danger" onclick="closeIdentityMenu(); signOut();">Sign out</button>
          </div>
        </div>
      </div>
      <div id="dash-root"><p class="dash-empty">Loading…</p></div>
      <div id="settings-root" class="hidden"></div>
    `;
  }}

  function goToDashboard() {{
    document.querySelector(".narrow-page").classList.add("hidden");
    const screen = document.getElementById("dashboard-screen");
    screen.innerHTML = dashboardScreen(currentEmail, avatarInitial(currentEmail));
    screen.classList.remove("hidden");
    mountDashboard(currentToken);
  }}

  function backToConnect() {{
    if (dashboardTimer) clearInterval(dashboardTimer);
    document.getElementById("dashboard-screen").classList.add("hidden");
    document.querySelector(".narrow-page").classList.remove("hidden");
  }}

  function toggleIdentityMenu(evt) {{
    evt.stopPropagation();
    const dd = document.getElementById("identity-dropdown");
    const opening = dd.classList.contains("hidden");
    dd.classList.toggle("hidden", !opening);
    evt.currentTarget.setAttribute("aria-expanded", opening ? "true" : "false");
  }}

  function closeIdentityMenu() {{
    const dd = document.getElementById("identity-dropdown");
    if (dd) dd.classList.add("hidden");
    const trigger = document.querySelector(".identity-trigger");
    if (trigger) trigger.setAttribute("aria-expanded", "false");
  }}

  // Closes the identity dropdown on any click outside it — cheap to
  // register once at load rather than per dashboardScreen() render,
  // since #identity-dropdown only exists (and only needs closing)
  // while the dashboard screen is mounted; closeIdentityMenu() itself
  // already no-ops safely when it doesn't.
  document.addEventListener("click", closeIdentityMenu);

  function copyCurrentToken() {{
    if (currentToken) navigator.clipboard.writeText(currentToken);
  }}

  function copyText(id) {{
    navigator.clipboard.writeText(document.getElementById(id).textContent);
  }}

  async function applyLocalConfig() {{
    const btn = document.getElementById("local-setup-btn");
    const result = document.getElementById("local-setup-result");
    btn.disabled = true;
    btn.textContent = "Applying…";
    try {{
      const res = await fetch("/setup/apply-local-config", {{
        method: "POST",
        headers: {{ Authorization: "Bearer " + currentToken }},
      }});
      const data = await res.json();
      if (res.ok && data.ok) {{
        result.innerHTML = '<span style="color:var(--ok);">&check; Done — wrote to <code>' + data.path + '</code>'
          + (data.backed_up_to ? ' (previous version backed up to <code>' + data.backed_up_to + '</code>)' : '')
          + '. Restart any running Claude Code sessions to pick it up.</span>';
        btn.textContent = "Applied";
      }} else {{
        result.innerHTML = '<span style="color:var(--err);">' + (data.error || "Something went wrong.") + '</span>';
        btn.disabled = false;
        btn.textContent = "Apply to my Claude Code config";
      }}
    }} catch (err) {{
      result.innerHTML = '<span style="color:var(--err);">' + err.message + '</span>';
      btn.disabled = false;
      btn.textContent = "Apply to my Claude Code config";
    }}
  }}

  // Fetches a personalized setup script (see /setup/local-script — same
  // bearer-token auth as every other protected route, never a plain link,
  // so the token-bearing file can only be produced by someone who's
  // already authenticated) and triggers a normal browser download via a
  // synthetic <a download> click. Nothing here talks to the local
  // filesystem — that only happens once the user runs the downloaded
  // script themselves.
  async function downloadLocalScript() {{
    const btn = document.getElementById("local-script-btn");
    const result = document.getElementById("local-script-result");
    const filename = "mcp-context-inspector-setup.py";
    btn.disabled = true;
    btn.textContent = "Preparing…";
    try {{
      const res = await fetch("/setup/local-script", {{
        headers: {{ Authorization: "Bearer " + currentToken }},
      }});
      if (!res.ok) {{
        const data = await res.json().catch(() => ({{}}));
        throw new Error(data.error || ("HTTP " + res.status));
      }}
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      result.innerHTML = '<span style="color:var(--ok);">&check; Downloaded <code>' + filename + '</code> — '
        + 'run it with:</span><pre style="margin-top:0.4rem;">python3 ' + filename + '</pre>'
        + '<span style="color:var(--text-dim);">It contains your token, so treat it like a password — delete it after running.</span>';
      btn.disabled = false;
      btn.textContent = "Download setup script";
    }} catch (err) {{
      result.innerHTML = '<span style="color:var(--err);">' + err.message + '</span>';
      btn.disabled = false;
      btn.textContent = "Download setup script";
    }}
  }}

  // --- Live dashboard ------------------------------------------------
  // Renders each authenticated caller's own sessions right on this page,
  // via the same /api/* routes and bearer token an LLM/agent uses — no
  // separate client needed to actually see what got recorded. Every
  // read here is already owner-scoped server-side (see
  // MultiTokenAuthMiddleware + metrics/store.py's owner filtering), so
  // this page can never show another user's data even if it tried to.
  //
  // KPI strip / range filter: fetches /api/sessions?limit=500 ONCE and
  // aggregates client-side (same "personal-project scale" assumption
  // already used elsewhere in this codebase) instead of adding new
  // backend aggregate endpoints. Every tile — sessions, tokens, spend,
  // cache hit rate, tool error rate, context alerts — is computed from
  // that single bulk list response; none require a per-session detail
  // fetch.

  const CATEGORY_COLORS = {{
    system: "var(--cat-system)", tools: "var(--cat-tools)", user: "var(--cat-user)",
    reasoning: "var(--cat-reasoning)", thinking: "var(--cat-thinking)",
    tool_call: "var(--cat-toolcall)", tool_result: "var(--cat-toolresult)", answer: "var(--cat-answer)",
  }};

  const SRC_BADGE = {{
    claude_code: {{cls: "cc", label: "CC"}},
    copilot: {{cls: "gh", label: "GH"}},
    bedrock_agent: {{cls: "bd", label: "BD"}},
  }};

  async function apiGet(token, path) {{
    const res = await fetch(path, {{ headers: {{ Authorization: "Bearer " + token }} }});
    if (!res.ok) throw new Error("HTTP " + res.status);
    return res.json();
  }}

  function fmtCost(v) {{ return "$" + (v || 0).toFixed(4); }}
  function fmtTokens(n) {{
    n = n || 0;
    if (n >= 1000000) return (n / 1000000).toFixed(1) + "M";
    if (n >= 1000) return (n / 1000).toFixed(1) + "k";
    return String(n);
  }}
  function timeAgo(ts) {{
    if (!ts) return "—";
    const secs = Math.max(0, Date.now() / 1000 - ts);
    if (secs < 60) return "just now";
    if (secs < 3600) return Math.floor(secs / 60) + "m ago";
    if (secs < 86400) return Math.floor(secs / 3600) + "h ago";
    return Math.floor(secs / 86400) + "d ago";
  }}

  let dashboardTimer = null;
  let dashboardSelected = null;
  let dashboardRange = "7d"; // "today" | "7d" | "30d" | "all"
  let dashboardSourceFilter = "all"; // "all" | "claude_code" | "copilot" | "bedrock_agent"
  let dashboardSessions = []; // full bulk list (up to 500), unfiltered

  const RANGE_SECONDS = {{today: 86400, "7d": 7 * 86400, "30d": 30 * 86400, all: null}};
  const SOURCE_LABELS = {{claude_code: "Claude Code", copilot: "Copilot", bedrock_agent: "Bedrock agent"}};

  // Mirrors mci_common.config.CONTEXT_WINDOW_TOKENS — duplicated here
  // rather than plumbed through the API response because every session
  // in the list already carries total_tokens, and pulling this one
  // constant server-side into the list endpoint isn't worth a new
  // response field. Keep in sync if that constant ever changes.
  const CONTEXT_WINDOW_TOKENS = 200000;

  function ctxPressure(totalTokens) {{
    const pct = Math.min(100, ((totalTokens || 0) / CONTEXT_WINDOW_TOKENS) * 100);
    const level = pct >= 95 ? "err" : pct >= 80 ? "warn" : "ok";
    return {{pct, level}};
  }}

  function sessionsInRange() {{
    const secs = RANGE_SECONDS[dashboardRange];
    const cutoff = secs === null ? null : Date.now() / 1000 - secs;
    return dashboardSessions.filter((s) => {{
      if (cutoff !== null && (s.timestamp || 0) < cutoff) return false;
      if (dashboardSourceFilter !== "all" && s.source !== dashboardSourceFilter) return false;
      return true;
    }});
  }}

  function renderKpiStrip() {{
    const inRange = sessionsInRange();
    const tokens = inRange.reduce((sum, s) => sum + (s.total_tokens || 0), 0);
    const spend = inRange.reduce((sum, s) => sum + (s.estimated_cost || 0), 0);
    const alertCount = inRange.filter((s) => ctxPressure(s.total_tokens).level !== "ok").length;
    const totalCacheRead = inRange.reduce((sum, s) => sum + (s.cache_read_tokens || 0), 0);
    const totalFreshInput = inRange.reduce((sum, s) => sum + (s.fresh_input_tokens || 0), 0);
    const cacheDenom = totalCacheRead + totalFreshInput;
    const cacheHitRate = cacheDenom ? Math.round((totalCacheRead / cacheDenom) * 100) + "%" : "—";
    const totalToolCalls = inRange.reduce((sum, s) => sum + (s.tool_call_total || 0), 0);
    const totalToolErrors = inRange.reduce((sum, s) => sum + (s.tool_call_errors || 0), 0);
    const toolErrorRate = totalToolCalls ? Math.round((totalToolErrors / totalToolCalls) * 100) + "%" : "—";
    return `
      <div class="kpi"><span class="kpi-label">Sessions</span><span class="kpi-value">` + inRange.length + `</span></div>
      <div class="kpi"><span class="kpi-label">Tokens</span><span class="kpi-value">` + fmtTokens(tokens) + `</span></div>
      <div class="kpi"><span class="kpi-label">Spend</span><span class="kpi-value">` + fmtCost(spend) + `</span></div>
      <div class="kpi"><span class="kpi-label">Cache hit rate</span><span class="kpi-value">` + cacheHitRate + `</span></div>
      <div class="kpi"><span class="kpi-label">Tool error rate</span><span class="kpi-value">` + toolErrorRate + `</span></div>
      <div class="kpi"><span class="kpi-label">Context alerts</span><span class="kpi-value` + (alertCount ? " accent-warn" : "") + `">` + alertCount + `<small> &ge;80% window</small></span></div>
    `;
  }}

  function renderRangeRow() {{
    const opts = [["today", "Today"], ["7d", "7d"], ["30d", "30d"], ["all", "All time"]];
    return `
      <div class="filter-row" style="padding: 0;">
        ` + opts.map(([key, label]) =>
          '<span class="chip' + (dashboardRange === key ? ' active' : '') + '" onclick="setDashboardRange(\\'' + key + '\\')">' + label + '</span>'
        ).join("") + `
      </div>`;
  }}

  function renderSourceFilterRow() {{
    const present = new Set(dashboardSessions.map((s) => s.source));
    const opts = [["all", "All sources"]].concat(
      Object.keys(SOURCE_LABELS).filter((k) => present.has(k)).map((k) => [k, SOURCE_LABELS[k]])
    );
    if (opts.length <= 1) return "";
    return `
      <div class="filter-row" style="padding: 0;">
        ` + opts.map(([key, label]) =>
          '<span class="chip' + (dashboardSourceFilter === key ? ' active' : '') + '" onclick="setSourceFilter(\\'' + key + '\\')">' + label + '</span>'
        ).join("") + `
      </div>`;
  }}

  function renderInsightStrip() {{
    const inRange = sessionsInRange();
    if (inRange.length < 2) return "";
    const cards = [];

    const pressureCount = inRange.filter((s) => ctxPressure(s.total_tokens).level !== "ok").length;
    cards.push(pressureCount
      ? '<div class="insight-card"><div class="i-label">Context pressure</div><div class="i-body"><strong>' + pressureCount + ' of ' + inRange.length + '</strong> sessions this period crossed 80% of the context window.</div></div>'
      : '<div class="insight-card"><div class="i-label">Context pressure</div><div class="i-body">No sessions this period crossed 80% of the context window.</div></div>');

    const bySource = {{}};
    inRange.forEach((s) => {{ bySource[s.source] = (bySource[s.source] || 0) + 1; }});
    const sources = Object.keys(bySource);
    if (sources.length) {{
      const topSource = sources.reduce((a, b) => (bySource[a] >= bySource[b] ? a : b));
      cards.push('<div class="insight-card"><div class="i-label">Busiest source</div><div class="i-body"><strong>' + (SOURCE_LABELS[topSource] || topSource) + '</strong> (' + bySource[topSource] + ' of ' + inRange.length + ' sessions this period).</div></div>');
    }}

    const spend = inRange.reduce((sum, s) => sum + (s.estimated_cost || 0), 0);
    cards.push('<div class="insight-card"><div class="i-label">Spend this period</div><div class="i-body"><strong>' + fmtCost(spend) + '</strong> across ' + inRange.length + ' sessions (' + fmtCost(spend / inRange.length) + ' avg).</div></div>');

    return '<div class="insight-strip">' + cards.join("") + '</div>';
  }}

  function renderQuotaStrip() {{
    // Neither window is wired to a real data source yet — see
    // docs/internal/OTLP_INTEGRATION_PLAN.md's "5-hour / 7-day usage-window
    // percentage" verdict (not achievable via any supported path right
    // now). Kept visually complete per that doc's framing, with the
    // pending-source badge baked into .quota-card.pending and no
    // fabricated percentage or fill width.
    const card = (label) => `
      <div class="quota-card pending">
        <div class="quota-top"><span class="q-label">` + label + `</span><span class="q-pct">&mdash;</span></div>
        <div class="quota-track"><div class="quota-fill" style="width:0%;"></div></div>
        <div class="quota-sub">Not yet wired to a data source — see project plan.</div>
      </div>`;
    return card("5h usage window") + card("7d usage window");
  }}

  function renderSessionRow(s) {{
    const prompt = (s.prompt || "(no prompt)").slice(0, 60);
    const active = s.session_id === dashboardSelected ? " active" : "";
    const badge = SRC_BADGE[s.source] || {{cls: "bd", label: "?"}};
    const pressure = ctxPressure(s.total_tokens);
    const dotTitle = pressure.level === "ok" ? "Context window usage: " + Math.round(pressure.pct) + "%"
      : "Context window usage: " + Math.round(pressure.pct) + "% — approaching the limit";
    return `
      <div class="session-row` + active + `" data-id="` + s.session_id + `" onclick="selectSession(event)">
        <div class="session-row-top">
          <span class="src-badge ` + badge.cls + `">` + badge.label + `</span>
          <span class="session-prompt">` + prompt + `</span>
          <span class="ctx-dot ` + pressure.level + `" title="` + dotTitle + `"></span>
        </div>
        <div class="session-row-meta">
          <span>` + timeAgo(s.timestamp) + ` &middot; ` + (s.turn_count ?? 0) + ` turn` + (s.turn_count === 1 ? "" : "s") + `</span>
          <span class="ctx-badge">` + fmtTokens(s.total_tokens) + ` tok &middot; ` + fmtCost(s.estimated_cost) + `</span>
        </div>
      </div>`;
  }}

  function renderSessionListPanel() {{
    const inRange = sessionsInRange();
    const body = !inRange.length
      ? '<p class="dash-empty">No sessions in view yet. Signing in here only grants query access to data recorded elsewhere — Claude Code/Copilot telemetry, or an agent calling record_session — it does not start recording this chat\\'s own activity. Set up telemetry (see the connect page) or call record_session and this list fills in automatically, no page refresh needed.</p>'
      : inRange.map(renderSessionRow).join("");
    return `
      <div class="panel">
        <div class="panel-head"><span class="panel-title">Sessions</span></div>
        <div id="source-filter-row">` + renderSourceFilterRow() + `</div>
        <div class="session-list" id="session-list">` + body + `</div>
      </div>`;
  }}

  function renderContextBar(timeline) {{
    const total = timeline.length ? timeline[timeline.length - 1].cumulative_tokens : 0;
    return timeline.map((b) => {{
      const color = CATEGORY_COLORS[b.category] || "var(--cat-system)";
      const pct = total ? (b.token_estimate / total * 100) : 0;
      return '<div style="width:' + pct + '%; background:' + color + ';"></div>';
    }}).join("");
  }}

  function escapeHtml(s) {{
    const div = document.createElement("div");
    div.textContent = s == null ? "" : String(s);
    return div.innerHTML;
  }}

  function toggleBlockDetail(idx) {{
    const row = document.getElementById("block-row-" + idx);
    const detail = document.getElementById("block-detail-" + idx);
    if (!row || !detail) return;
    const opening = detail.classList.contains("hidden");
    detail.classList.toggle("hidden", !opening);
    row.classList.toggle("expanded", opening);
  }}

  function renderContextBlockRow(b, idx) {{
    const color = CATEGORY_COLORS[b.category] || "var(--cat-system)";
    const label = b.status === "redacted"
      ? '<span class="redacted">' + (b.label || b.category) + ' (redacted)</span>'
      : (b.label || b.category);
    const hasContent = typeof b.content === "string" && b.content.length > 0;
    const detail = hasContent
      ? '<div class="block-detail hidden" id="block-detail-' + idx + '">' + escapeHtml(b.content) + '</div>'
      : '<div class="block-detail unavailable hidden" id="block-detail-' + idx + '">' +
        (b.status === "redacted"
          ? "Content is redacted by the client itself before export — not available here either."
          : "Content wasn't captured for this block (recorded before this feature existed, or via record_session without the optional field).") +
        '</div>';
    return `
      <div class="block-row" id="block-row-` + idx + `" onclick="toggleBlockDetail(` + idx + `)">
        <span class="block-chev">&#9656;</span>
        <span class="block-dot" style="background:` + color + `;"></span>
        <span class="block-label">` + label + `</span>
        <span class="block-tok">` + b.token_estimate + ` tok</span>
        <span class="block-pct">` + b.cumulative_pct + `%</span>
      </div>
      ` + detail;
  }}

  function renderContextTab(timeline) {{
    if (!timeline.length) {{
      return '<p class="dash-empty">No context_blocks for this session — record_session was called without the optional field.</p>';
    }}
    return `
      <div class="agent-tabs">
        <span class="agent-tab active">main<span class="a-tok">` + fmtTokens(timeline[timeline.length - 1].cumulative_tokens) + ` tok</span></span>
      </div>
      <div class="ctx-bar">` + renderContextBar(timeline) + `</div>
      <div class="ctx-legend">
        <span><i style="background:var(--cat-system);"></i>system</span>
        <span><i style="background:var(--cat-tools);"></i>tools</span>
        <span><i style="background:var(--cat-user);"></i>user</span>
        <span><i style="background:var(--cat-reasoning);"></i>reasoning</span>
        <span><i style="background:var(--cat-thinking);"></i>thinking</span>
        <span><i style="background:var(--cat-toolcall);"></i>tool call</span>
        <span><i style="background:var(--cat-toolresult);"></i>tool result</span>
        <span><i style="background:var(--cat-answer);"></i>answer</span>
      </div>
      <div class="section-heading">Context blocks</div>
      <div class="block-list">` + timeline.map((b, i) => renderContextBlockRow(b, i)).join("") + `</div>
    `;
  }}

  function renderOverviewTab(detail) {{
    const m = detail.metrics.prompt_metrics;
    // "Lines changed" / "Active time" from the mockup have no backing
    // schema field — omitted rather than shown as fake zeros.
    return `
      <div class="agent-tabs">
        <span class="agent-tab active">main</span>
      </div>
      <div class="metric-grid">
        <div class="metric-tile"><div class="m-label">Tokens</div><div class="m-value">` + fmtTokens(m.total_tokens) + `</div></div>
        <div class="metric-tile accent-ok"><div class="m-label">Cache hit</div><div class="m-value">` + cacheHitPct(detail.turns) + `</div></div>
        <div class="metric-tile"><div class="m-label">Cost</div><div class="m-value">` + fmtCost(m.estimated_cost) + `</div></div>
        <div class="metric-tile"><div class="m-label">Tool calls</div><div class="m-value">` + m.tool_call_count + `</div></div>
      </div>
    `;
  }}

  function cacheHitPct(turns) {{
    // Anthropic's usage accounting: cache_read_input_tokens is a
    // SEPARATE bucket from input_tokens (the fresh/uncached portion),
    // not a subset of it — a turn that's almost entirely served from
    // cache can have cache_read_input_tokens far exceed input_tokens
    // (e.g. read=22134, input=2). Dividing read/input (found via a live
    // browser E2E test — a real session rendered "4184%") can exceed
    // 100%; the correct hit rate is read's share of the turn's TOTAL
    // input (read + fresh), which is always <= 100%.
    if (!turns || !turns.length) return "—";
    let read = 0, fresh = 0;
    turns.forEach((t) => {{ read += t.cache_read_input_tokens || 0; fresh += t.input_tokens || 0; }});
    const total = read + fresh;
    if (!total) return "—";
    return Math.round((read / total) * 100) + "%";
  }}

  function statusBadge(status) {{
    const cls = status === "success" || status === "ok" ? "ok" : (status === "error" ? "err" : "dim");
    return '<span class="badge-pill ' + cls + '"><span class="status-dot ' + cls + '"></span>' + status + '</span>';
  }}

  function renderToolsTab(trace) {{
    if (!trace.length) {{
      return '<p class="dash-empty">No tool calls recorded for this session.</p>';
    }}
    const rows = trace.map((c, i) => `
      <div class="tool-row">
        <span class="mono">` + (i + 1) + `</span>
        <span>` + c.tool + `</span>
        <span>` + statusBadge(c.status) + `</span>
        <span class="mono">` + (c.latency_ms || 0) + `ms</span>
        <span class="args mono">` + JSON.stringify(c.args || {{}}).slice(0, 80) + `</span>
        <span class="mono">` + timeAgo(c.timestamp) + `</span>
      </div>`).join("");
    return `
      <div class="tool-table">
        <div class="tool-row tool-head"><span>#</span><span>Tool</span><span>Status</span><span>Latency</span><span>Args</span><span>Time</span></div>
        ` + rows + `
      </div>`;
  }}

  function renderReliabilitySubpanel(trace) {{
    // Real data — computed client-side from this session's already-
    // fetched trace (grouped by tool, ok vs. error counts). Nothing new
    // added server-side for this.
    if (!trace.length) {{
      return '<div class="subpanel"><h4>Tool reliability, this session</h4><p class="dash-empty">No tool calls recorded.</p></div>';
    }}
    const byTool = {{}};
    trace.forEach((c) => {{
      const t = byTool[c.tool] || (byTool[c.tool] = {{ok: 0, err: 0}});
      if (c.status === "success" || c.status === "ok") t.ok += 1; else t.err += 1;
    }});
    const rows = Object.entries(byTool).map(([tool, counts]) => {{
      const total = counts.ok + counts.err;
      const errPct = total ? (counts.err / total * 100) : 0;
      return `
        <div class="bar-row">
          <span class="b-name">` + tool + `</span>
          <div class="bar-track"><div class="bar-fill` + (errPct > 0 ? " err" : "") + `" style="width:` + (100 - errPct) + `%;"></div></div>
          <span class="b-val">` + counts.ok + `/` + total + `</span>
        </div>`;
    }}).join("");
    return '<div class="subpanel"><h4>Tool reliability, this session</h4>' + rows + '</div>';
  }}

  function renderBreakdownTab(trace) {{
    const notTracked = (title) => '<div class="subpanel"><h4>' + title + '</h4><p class="dash-empty">Not tracked yet.</p></div>';
    return `
      <div class="two-col">
        ` + renderReliabilitySubpanel(trace) + `
        ` + notTracked("Spend by subagent / skill") + `
        ` + notTracked("MCP server connections") + `
        ` + notTracked("Reliability signals / API errors") + `
      </div>
    `;
  }}

  function renderSessionDetail(sessionId, detail, timeline) {{
    const s = detail.metrics.session;
    const m = detail.metrics.prompt_metrics;
    return `
      <div class="panel">
        <div class="detail-head">
          <div class="detail-title">` + (m.prompt || "(no prompt)").slice(0, 90) + `</div>
          <div class="detail-meta">
            <span>` + s.source + `</span>
            <span>` + s.status + `</span>
            <span>` + s.model + `</span>
            <span>` + timeAgo(s.timestamp) + `</span>
          </div>
        </div>
        <div class="tabs">
          <div class="tab active" data-tab="overview" onclick="showDetailTab('overview', this)">Overview</div>
          <div class="tab" data-tab="context" onclick="showDetailTab('context', this)">Context Explorer</div>
          <div class="tab" data-tab="tools" onclick="showDetailTab('tools', this)">Tool calls</div>
          <div class="tab" data-tab="breakdown" onclick="showDetailTab('breakdown', this)">Breakdown</div>
        </div>
        <div class="tab-content active" data-content="overview">` + renderOverviewTab(detail) + `</div>
        <div class="tab-content" data-content="context">` + renderContextTab(timeline) + `</div>
        <div class="tab-content" data-content="tools">` + renderToolsTab(detail.trace) + `</div>
        <div class="tab-content" data-content="breakdown">` + renderBreakdownTab(detail.trace) + `</div>
      </div>
    `;
  }}

  function showDetailTab(name, btn) {{
    const panel = btn.closest(".panel");
    if (!panel) return;
    panel.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
    panel.querySelectorAll(".tab-content").forEach((c) => c.classList.toggle("active", c.dataset.content === name));
  }}

  async function selectSession(evt) {{
    const sessionId = evt.currentTarget.dataset.id;
    dashboardSelected = sessionId;
    document.querySelectorAll(".session-row").forEach((r) => r.classList.toggle("active", r.dataset.id === sessionId));
    const detailEl = document.getElementById("detail-panel");
    if (!detailEl) return;
    detailEl.innerHTML = '<p class="dash-empty">Loading…</p>';
    try {{
      const token = document.getElementById("dash-root").dataset.token;
      const [detail, timeline] = await Promise.all([
        apiGet(token, "/api/sessions/" + sessionId),
        apiGet(token, "/api/context-timeline/" + sessionId),
      ]);
      detailEl.innerHTML = renderSessionDetail(sessionId, detail, timeline);
    }} catch (err) {{
      detailEl.innerHTML = '<p class="dash-error">Failed to load session: ' + err.message + '</p>';
    }}
  }}

  function renderDashboardShell() {{
    const root = document.getElementById("dash-root");
    if (!root) return;
    root.innerHTML = `
      <div class="layout">
        <div id="range-row">` + renderRangeRow() + `</div>
        <div class="kpi-strip" id="kpi-strip">` + renderKpiStrip() + `</div>
        <div class="quota-strip">` + renderQuotaStrip() + `</div>
        <div id="insight-strip">` + renderInsightStrip() + `</div>
        <div class="body-grid">
          <div id="session-list-panel">` + renderSessionListPanel() + `</div>
          <div id="detail-panel"><div class="panel"><p class="dash-empty">Select a session.</p></div></div>
        </div>
      </div>
    `;
  }}

  function refreshDashboardPanels() {{
    document.getElementById("kpi-strip").innerHTML = renderKpiStrip();
    document.getElementById("insight-strip").innerHTML = renderInsightStrip();
    document.getElementById("session-list-panel").innerHTML = renderSessionListPanel();
  }}

  function setDashboardRange(range) {{
    dashboardRange = range;
    document.getElementById("range-row").innerHTML = renderRangeRow();
    refreshDashboardPanels();
  }}

  function setSourceFilter(source) {{
    dashboardSourceFilter = source;
    refreshDashboardPanels();
  }}

  async function refreshDashboard(token) {{
    const root = document.getElementById("dash-root");
    if (!root) {{ clearInterval(dashboardTimer); return; }}
    try {{
      dashboardSessions = await apiGet(token, "/api/sessions?limit=500");
      if (!document.getElementById("kpi-strip")) {{
        renderDashboardShell();
      }} else {{
        refreshDashboardPanels();
      }}
      const listEl = document.getElementById("session-list");
      if (listEl && !sessionsInRange().some((s) => s.session_id === dashboardSelected)) {{
        const row = listEl.querySelector(".session-row");
        if (row) row.click();
      }}
    }} catch (err) {{
      root.innerHTML = '<p class="dash-error">Failed to load sessions: ' + err.message + '</p>';
    }}
  }}

  // One poll loop per page load; re-mounting (e.g. signing in again)
  // clears the previous timer instead of stacking a second one.
  function mountDashboard(token) {{
    const root = document.getElementById("dash-root");
    if (root) root.dataset.token = token;
    dashboardSelected = null;
    dashboardSessions = [];
    if (dashboardTimer) clearInterval(dashboardTimer);
    renderDashboardShell();
    refreshDashboard(token);
    dashboardTimer = setInterval(() => refreshDashboard(token), 8000);
  }}

  // --- Project settings (new UI, no backend yet) ----------------------
  // TODO: wire to a real per-project settings endpoint once one exists.
  // Every control below is inert — this screen exists so the settings
  // UX is visually complete and navigable via the ⚙ toggle, matching
  // the mockup, but nothing here persists across a page reload.
  function renderSettingsScreen() {{
    return `
      <div class="settings-wrap">
        <div class="settings-head">
          <h3>Project settings</h3>
          <p>Alert thresholds, redaction/retention, and session labels. Nothing here is wired to a backend yet —
          changes made here are not saved.</p>
        </div>
        <div class="disclosure-note">
          <span>&#9888;</span>
          <span><strong>Not yet persisted.</strong> This screen is UI-complete but every control below is
          disconnected from a real settings store — reloading the page resets it.</span>
        </div>
        <div class="panel">
          <div class="panel-body">
            <div class="config-row">
              <div class="config-copy">
                <div class="c-title">Context window alert threshold</div>
                <div class="c-desc">Flag a session once its context usage crosses this percentage.</div>
              </div>
              <div class="config-control">
                <input class="cfg-input" type="number" value="80" disabled />
                <span>%</span>
              </div>
            </div>
            <div class="config-row">
              <div class="config-copy">
                <div class="c-title">Tool error rate alert</div>
                <div class="c-desc">Flag a session once its tool error rate crosses this percentage.</div>
              </div>
              <div class="config-control">
                <input class="cfg-input" type="number" value="20" disabled />
                <span>%</span>
              </div>
            </div>
            <div class="config-row">
              <div class="config-copy">
                <div class="c-title">Redact raw request/response bodies</div>
                <div class="c-desc">Applies to the OTLP raw-content opt-in (Claude Code / Copilot). Off by default.</div>
              </div>
              <div class="config-control">
                <label class="switch"><input type="checkbox" disabled /><span class="switch-track"></span></label>
              </div>
            </div>
            <div class="config-row">
              <div class="config-copy">
                <div class="c-title">Session data retention</div>
                <div class="c-desc">How long recorded sessions are kept before being eligible for deletion.</div>
              </div>
              <div class="config-control">
                <select class="cfg-select" disabled>
                  <option>30 days</option>
                  <option>90 days</option>
                  <option>Forever</option>
                </select>
              </div>
            </div>
            <div class="config-row">
              <div class="config-copy">
                <div class="c-title">Session labels</div>
                <div class="c-desc">Tags to help you filter sessions later.</div>
              </div>
              <div class="config-control">
                <div class="tag-input-row">
                  <span class="tag">production</span>
                  <span class="tag">staging</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    `;
  }}

  function toggleSettings() {{
    const dashRoot = document.getElementById("dash-root");
    const settingsRoot = document.getElementById("settings-root");
    if (!dashRoot || !settingsRoot) return;
    const showingSettings = settingsRoot.classList.contains("hidden");
    if (showingSettings) {{
      settingsRoot.innerHTML = renderSettingsScreen();
      dashRoot.classList.add("hidden");
      settingsRoot.classList.remove("hidden");
    }} else {{
      settingsRoot.classList.add("hidden");
      settingsRoot.innerHTML = "";
      dashRoot.classList.remove("hidden");
    }}
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
  let currentEmail = null;
  let currentToken = null;

  function consentPage(email) {{
    const initial = avatarInitial(email);
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
      <p class="card-hint" style="text-align:center; margin: -0.6rem 0 1.4rem;">
        Your token, your sessions, your local Claude Code config — all of it stays on this
        computer. Nothing you set up below is ever sent anywhere except this server.
      </p>
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
        persistSession(data.mcp_token, data.email);
        currentEmail = data.email;
        currentToken = data.mcp_token;
        landing.innerHTML = successBanner(data.email) + connectPage(data.email, data.mcp_token);
      }} else {{
        landing.innerHTML = "<div class='card security'>Sign-in failed: " + (data.error || "unknown error") + "</div>";
      }}
    }} catch (err) {{
      pendingCredential = null;
      landing.innerHTML = "<div class='card security'>Sign-in failed: " + err.message + "</div>";
    }}
  }}

  // --- Browser persistence ---------------------------------------------
  // localStorage (not sessionStorage) — deliberately survives closing
  // the browser entirely, same trust model as staying signed into any
  // other Google-backed site: whoever authorized here once sees their
  // own dashboard again next visit with no re-auth, until they sign out
  // or the token is revoked server-side (see the README's "Can this
  // token be revoked?"). Nothing else is ever stored here — the token
  // itself is the only credential, same one shown in the "Your
  // connection" card and handed to your MCP client's config.
  const SS_TOKEN = "mci_token";
  const SS_EMAIL = "mci_email";

  function persistSession(token, email) {{
    localStorage.setItem(SS_TOKEN, token);
    localStorage.setItem(SS_EMAIL, email);
  }}

  function signOut() {{
    localStorage.removeItem(SS_TOKEN);
    localStorage.removeItem(SS_EMAIL);
    pendingCredential = null;
    currentEmail = null;
    currentToken = null;
    dashboardSelected = null;
    backToConnect();
    document.getElementById("dashboard-screen").innerHTML = "";
    document.getElementById("landing").classList.add("hidden");
    document.getElementById("landing").innerHTML = "";
    document.getElementById("intro").classList.remove("hidden");
  }}

  // Runs once on load. A stored token is trusted enough to go straight
  // to the dashboard screen (no flash of the sign-in screen for a
  // returning visitor), but then verified with a real request — a
  // token revoked server-side since the last visit signs this browser
  // back out instead of showing a dashboard that just 401s on every
  // fetch. The connect/config cards render into #landing too (kept
  // hidden) so "Token & config" on the dashboard topbar has content
  // ready without a page reload.
  function rehydrateFromStorage() {{
    const token = localStorage.getItem(SS_TOKEN);
    const email = localStorage.getItem(SS_EMAIL);
    if (!token || !email) return;
    currentEmail = email;
    currentToken = token;
    document.getElementById("intro").classList.add("hidden");
    const landing = document.getElementById("landing");
    landing.classList.remove("hidden");
    landing.innerHTML = successBanner(email) + connectPage(email, token);
    goToDashboard();
    apiGet(token, "/api/sessions?limit=1").catch(() => signOut());
  }}

  rehydrateFromStorage();
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


# --- OAuth 2.1 + PKCE authorization server ------------------------------
# The /oauth/* + /.well-known/* routes themselves live in
# mcp_server/routes/oauth.py. Lets an MCP client that only knows how to
# do OAuth (e.g. a "Connectors" style integration in a chat UI, which
# offers no way to paste a plain bearer token) authenticate anyway. This
# server acts as BOTH the OAuth resource server (the /mcp endpoint
# itself, gated by MultiTokenAuthMiddleware above) and the authorization
# server — the MCP spec allows either a combined or split deployment,
# and combined is simplest here since there's no separate identity
# provider to delegate to beyond Google sign-in, which this server
# already does its own verification of.
#
# Flow (RFC 8414 authorization server metadata, RFC 9728 protected
# resource metadata, RFC 7591 dynamic client registration, OAuth 2.1 +
# PKCE for the actual grant):
#   1. Client hits /mcp with no/bad token -> 401 with a WWW-Authenticate
#      header pointing at /.well-known/oauth-protected-resource/mcp (see
#      MultiTokenAuthMiddleware above).
#   2. Client fetches that, learns this server is also its own
#      authorization server, fetches /.well-known/oauth-authorization-server
#      for the actual endpoints.
#   3. Client POSTs /oauth/register once (RFC 7591) to get a client_id —
#      no manual setup, no client secret (public client, PKCE-protected).
#   4. Client opens /oauth/authorize in a browser; user signs in with
#      Google (this server's existing identity check, reused as the
#      consent step); server redirects back with a one-time code.
#   5. Client exchanges the code at /oauth/token (with the PKCE verifier)
#      for an access token — which is just an ordinary bearer token, so
#      MultiTokenAuthMiddleware needs no special-casing to accept it (see
#      auth_store.is_valid_token, which checks both sign-in tokens and
#      OAuth-issued ones).
#
# All of this is generic, spec-compliant OAuth — any MCP client that does
# discovery correctly can use it, not just one specific product.


