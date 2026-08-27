// mcp-context-inspector: mobile session-history webapp. Vanilla JS,
// no framework/build step (matches the project's existing zero-build
// philosophy for the desktop dashboard's own inline JS in
// mcp_server/routes/auth.py). Read-only: lists sessions from
// GET /api/sessions and renders a session's detail from
// GET /api/sessions/{id} and GET /api/context-timeline/{id}.

(function () {
  "use strict";

  // --- Auth / storage ---------------------------------------------------
  // Namespaced distinctly from the desktop dashboard's own localStorage
  // keys (mci_token / mci_email, see routes/auth.py's persistSession) so
  // signing into one surface doesn't collide with or clobber the other.
  // they're deliberately independent sessions even though both ultimately
  // hold an MCP bearer token for the same account.
  const LS_TOKEN = "mciw_token";
  const LS_EMAIL = "mciw_email";

  const SRC_BADGE = {
    claude_code: { cls: "cc", label: "CC" },
    copilot: { cls: "gh", label: "GH" },
    bedrock_agent: { cls: "bd", label: "BD" },
  };

  // Mirrors the category colors in mcp_server/routes/auth.py's
  // CATEGORY_COLORS (--cat-* custom properties from styles.css).
  const CATEGORY_COLORS = {
    system: "var(--cat-system)",
    tools: "var(--cat-tools)",
    user: "var(--cat-user)",
    reasoning: "var(--cat-reasoning)",
    thinking: "var(--cat-thinking)",
    tool_call: "var(--cat-toolcall)",
    tool_result: "var(--cat-toolresult)",
    answer: "var(--cat-answer)",
  };

  function getToken() {
    return localStorage.getItem(LS_TOKEN);
  }

  function persistSession(token, email) {
    localStorage.setItem(LS_TOKEN, token);
    if (email) localStorage.setItem(LS_EMAIL, email);
  }

  function signOut() {
    localStorage.removeItem(LS_TOKEN);
    localStorage.removeItem(LS_EMAIL);
    location.hash = "";
    render();
  }
  window.signOut = signOut;

  // Picks up a one-time token handoff from /auth/login: after Google
  // sign-in there, if the page was reached via ?return_to=/m it appends
  // #token=...&email=... to the redirect back here. Read once, stored,
  // then stripped from the URL bar so it never lingers in history/logs.
  function consumeTokenFromLocation() {
    if (!location.hash || location.hash.length < 2) return false;
    const params = new URLSearchParams(location.hash.slice(1));
    const token = params.get("token");
    if (!token) return false;
    const email = params.get("email");
    persistSession(token, email);
    history.replaceState(null, "", location.pathname + location.search);
    return true;
  }

  function redirectToLogin() {
    const returnTo = encodeURIComponent(location.pathname);
    location.href = "/auth/login?return_to=" + returnTo;
  }

  // --- API ---------------------------------------------------------------
  async function apiGet(path) {
    const token = getToken();
    const res = await fetch(path, { headers: { Authorization: "Bearer " + token } });
    if (res.status === 401) {
      signOut();
      redirectToLogin();
      throw new Error("unauthorized");
    }
    if (!res.ok) throw new Error("HTTP " + res.status);
    return res.json();
  }

  // --- Formatting helpers (mirrors routes/auth.py's dashboard JS) -------
  function fmtCost(v) {
    return "$" + (v || 0).toFixed(4);
  }

  function fmtTokens(n) {
    n = n || 0;
    if (n >= 1000000) return (n / 1000000).toFixed(1) + "M";
    if (n >= 1000) return (n / 1000).toFixed(1) + "k";
    return String(n);
  }

  function timeAgo(ts) {
    if (!ts) return "n/a";
    const secs = Math.max(0, Date.now() / 1000 - ts);
    if (secs < 60) return "just now";
    if (secs < 3600) return Math.floor(secs / 60) + "m ago";
    if (secs < 86400) return Math.floor(secs / 3600) + "h ago";
    return Math.floor(secs / 86400) + "d ago";
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  // --- Rendering -----------------------------------------------------
  const app = document.getElementById("app");
  const refreshBtn = document.getElementById("refresh-btn");
  const signoutBtn = document.getElementById("signout-btn");

  function showChrome(signedIn) {
    refreshBtn.classList.toggle("hidden", !signedIn);
    signoutBtn.classList.toggle("hidden", !signedIn);
  }

  function renderGate() {
    showChrome(false);
    app.innerHTML = `
      <div class="gate">
        <div class="mark-lg">◈</div>
        <h1>Sign in to view your sessions</h1>
        <p>Your session history, cost, and token usage, read-only and on the go. Sign in with the same Google account you use on the desktop dashboard.</p>
        <button class="btn-primary" onclick="(${redirectToLogin.toString()})()">Sign in with Google</button>
      </div>
    `;
  }

  function renderLoading() {
    app.innerHTML = `<div class="state-msg">Loading…</div>`;
  }

  function renderError(message) {
    app.innerHTML = `<div class="state-msg error">${escapeHtml(message)}</div>`;
  }

  function sessionRowHtml(s) {
    const badge = SRC_BADGE[s.source] || { cls: "bd", label: "?" };
    const prompt = escapeHtml((s.prompt || "(no prompt)").slice(0, 80));
    return `
      <button class="session-row" onclick="location.hash = 'session/${encodeURIComponent(s.session_id)}'">
        <div class="session-row-top">
          <span class="src-badge ${badge.cls}">${badge.label}</span>
          <span class="session-prompt">${prompt}</span>
        </div>
        <div class="session-row-bottom">
          <span>${timeAgo(s.timestamp)} &middot; ${s.turn_count ?? 0} turn${s.turn_count === 1 ? "" : "s"}</span>
          <span class="ctx-badge">${fmtTokens(s.total_tokens)} tok &middot; ${fmtCost(s.estimated_cost)}</span>
        </div>
      </button>
    `;
  }

  async function renderList() {
    showChrome(true);
    renderLoading();
    try {
      const sessions = await apiGet("/api/sessions?limit=50");
      if (!sessions.length) {
        app.innerHTML = `<div class="state-msg">No sessions recorded yet.</div>`;
        return;
      }
      app.innerHTML = `<div class="session-list">${sessions.map(sessionRowHtml).join("")}</div>`;
    } catch (err) {
      if (err.message !== "unauthorized") renderError("Couldn't load sessions: " + err.message);
    }
  }

  function entryRowHtml(category, label, tokens, sub) {
    const color = CATEGORY_COLORS[category] || "var(--cat-system)";
    return `
      <div class="entry-row">
        <span class="entry-dot" style="background:${color};"></span>
        <div class="entry-body">
          <div class="entry-top">
            <span class="entry-label">${escapeHtml(label)}</span>
            ${tokens != null ? `<span class="entry-tok">${fmtTokens(tokens)} tok</span>` : ""}
          </div>
          ${sub ? `<div class="entry-sub">${escapeHtml(sub)}</div>` : ""}
        </div>
      </div>
    `;
  }

  const CATEGORY_TITLE = {
    system: "System", tools: "Tools", user: "User",
    reasoning: "Reasoning", thinking: "Thinking",
    tool_call: "Tool call", tool_result: "Tool result", answer: "Answer",
  };

  async function renderDetail(sessionId) {
    showChrome(true);
    renderLoading();
    try {
      const [detail, timeline] = await Promise.all([
        apiGet("/api/sessions/" + encodeURIComponent(sessionId)),
        apiGet("/api/context-timeline/" + encodeURIComponent(sessionId)).catch(() => []),
      ]);
      const s = detail.metrics.session;
      const m = detail.metrics.prompt_metrics;
      const trace = detail.trace || [];

      const cacheTotal = (m.input_tokens || 0);
      const cacheReadTotal = (detail.turns || []).reduce((sum, t) => sum + (t.cache_read_input_tokens || 0), 0);
      const cacheHitPct = cacheTotal + cacheReadTotal > 0
        ? Math.round((cacheReadTotal / (cacheTotal + cacheReadTotal)) * 100)
        : null;

      const kpis = `
        <div class="kpi-grid">
          <div class="kpi-tile"><div class="kpi-label">Tokens</div><div class="kpi-value">${fmtTokens(m.total_tokens)}</div></div>
          <div class="kpi-tile"><div class="kpi-label">Cost</div><div class="kpi-value">${fmtCost(m.estimated_cost)}</div></div>
          <div class="kpi-tile"><div class="kpi-label">Tool calls</div><div class="kpi-value">${m.tool_call_count ?? 0}</div></div>
          <div class="kpi-tile"><div class="kpi-label">Cache hit</div><div class="kpi-value">${cacheHitPct != null ? cacheHitPct + "%" : "n/a"}</div></div>
        </div>
      `;

      // Flat, ordered list: context blocks if the timeline has data,
      // else fall back to the tool-call trace. Either way, a single
      // scrollable list with type/color coding, not the desktop's
      // tabbed Context Explorer (out of scope for this read-only view).
      let entries;
      if (Array.isArray(timeline) && timeline.length) {
        entries = timeline
          .map((b) => entryRowHtml(b.category, b.label || CATEGORY_TITLE[b.category] || b.category, b.token_estimate, b.status))
          .join("");
      } else if (trace.length) {
        entries = trace
          .map((t) => entryRowHtml(t.status === "error" ? "tool_call" : "tool_result", t.tool, null, t.status + (t.latency_ms ? ` · ${t.latency_ms}ms` : "")))
          .join("");
      } else {
        entries = `<div class="state-msg">No context blocks or tool calls recorded for this session.</div>`;
      }

      const badge = SRC_BADGE[s.source] || { cls: "bd", label: "?" };

      app.innerHTML = `
        <div class="detail-header">
          <a class="back-link" href="#" onclick="location.hash=''; return false;">&larr; Sessions</a>
          <div class="detail-title">${escapeHtml((m.prompt || "(no prompt)").slice(0, 140))}</div>
          <div class="detail-meta">
            <span class="src-badge ${badge.cls}">${badge.label}</span>
            <span>${escapeHtml(s.model || "")}</span>
            <span>&middot; ${timeAgo(s.timestamp)}</span>
          </div>
        </div>
        ${kpis}
        <div class="section-label">Context / tool-call timeline</div>
        <div class="entry-list">${entries}</div>
      `;
    } catch (err) {
      if (err.message !== "unauthorized") renderError("Couldn't load session: " + err.message);
    }
  }

  // --- Router (hash-based, no build step / no framework needed) ------
  let currentRoute = null;

  function render() {
    if (!getToken()) {
      renderGate();
      return;
    }
    const hash = location.hash.replace(/^#/, "");
    currentRoute = hash;
    if (hash.startsWith("session/")) {
      renderDetail(decodeURIComponent(hash.slice("session/".length)));
    } else {
      renderList();
    }
  }

  function handleRefresh() {
    refreshBtn.classList.add("loading");
    render();
    setTimeout(() => refreshBtn.classList.remove("loading"), 400);
  }
  window.handleRefresh = handleRefresh;

  window.addEventListener("hashchange", render);

  // --- Boot ------------------------------------------------------------
  consumeTokenFromLocation();
  if (!getToken()) {
    redirectToLogin();
  } else {
    render();
  }
})();
