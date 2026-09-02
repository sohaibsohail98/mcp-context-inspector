// Extracted verbatim from mcp_server/routes/auth.py's auth_login handler
// (was two inline <script> blocks authored inside Python f-strings, with
// every { and } doubled). Behaviour is unchanged; the only edit is that
// the server-injected canonical origin is now read from window.__CFG__
// (set by the one <script> block auth_login still emits) instead of being
// string-interpolated here. See routes/auth.py for how this file is served.

// --- Landing hero demo-card animation ------------------------------
// Animates the hero demo card's bars/KPIs from zero once, on first
  // load only. A static mock would otherwise read as a screenshot,
  // not a live product. Respects prefers-reduced-motion by snapping
  // straight to final values instead of animating.
  (function () {
    var reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    var bars = document.querySelectorAll("#ctxwindow-demo-bar > div");
    var targets = { tokens: 48200, cache: 81, cost: 0.94, context: 84 };
    var duration = reduceMotion ? 0 : 900;
    var start = null;

    function ease(t) { return 1 - Math.pow(1 - t, 3); }

    function frame(ts) {
      if (start === null) start = ts;
      var t = duration === 0 ? 1 : Math.min(1, (ts - start) / duration);
      var e = ease(t);
      bars.forEach(function (bar) {
        var target = parseFloat(bar.dataset.w);
        bar.style.width = (target * e) + "%";
      });
      document.getElementById("kpi-tokens").textContent = (targets.tokens * e / 1000).toFixed(1) + "k";
      document.getElementById("kpi-cache").textContent = Math.round(targets.cache * e) + "%";
      document.getElementById("kpi-cost").textContent = "$" + (targets.cost * e).toFixed(2);
      document.getElementById("kpi-context").textContent = Math.round(targets.context * e) + "%";
      if (t < 1) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  })();

  // Server-injected canonical origin (PUBLIC_ORIGIN), so the connector
  // URL / config snippet / OTLP endpoint / curl + install commands are
  // always https://ctxwindow.uk/... regardless of the host this page
  // was loaded from. window.location.origin is still used for
  // same-origin fetch()es (relative paths would do too) and for the
  // localhost-detection UI branch.
  const canonicalOrigin = window.__CFG__.canonicalOrigin;
  const mcpUrl = canonicalOrigin + "/mcp";

  function connectPage(email, token) {
    const isLocalHost = ["localhost", "127.0.0.1"].includes(window.location.hostname);
    const claudeConfig = JSON.stringify({
      mcpServers: {
        "context-inspector": {
          url: mcpUrl,
          headers: { Authorization: "Bearer " + token }
        }
      }
    }, null, 2);
    const rawHeader = "Authorization: Bearer " + token;
    const curlCmd = 'curl -H "Authorization: Bearer ' + token + '" ' + canonicalOrigin + '/api/sessions';
    const otlpUrl = canonicalOrigin + "/otlp";
    const claudeOtelSnippet = [
      "export CLAUDE_CODE_ENABLE_TELEMETRY=1",
      "export OTEL_LOGS_EXPORTER=otlp",
      "export OTEL_METRICS_EXPORTER=otlp",
      "export OTEL_EXPORTER_OTLP_PROTOCOL=http/json",
      "export OTEL_EXPORTER_OTLP_ENDPOINT=" + otlpUrl,
      'export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer ' + token + '"',
      // Claude Code's exporter never sets service.name itself, and that is the
      // primary signal detect_vendor() matches on (see
      // mcp_server/otlp/__init__.py), so without this line every real
      // session falls back to detect_vendor's session.id-presence check,
      // which is itself only populated by the two INCLUDE_SESSION_ID
      // vars below. Omitting any of these four means every session from
      // this snippet lands in recent_skipped, not your dashboard.
      // found in review after local_setup.py's installer already
      // carried all four but this manual snippet didn't.
      "export OTEL_RESOURCE_ATTRIBUTES=service.name=claude-code",
      "export OTEL_METRICS_INCLUDE_SESSION_ID=true",
      "export OTEL_LOGS_INCLUDE_SESSION_ID=true",
      "export OTEL_LOGS_EXPORT_INTERVAL=5000",
    ].join("\n");
    const claudeOtelOptin = [
      "export OTEL_LOG_RAW_API_BODIES=1",
      // Claude Code truncates any content-bearing attribute (including this
      // raw body) at 60KB by default. Real sessions with a system prompt
      // and tool specs exceed that almost immediately, which truncates the
      // body's JSON mid-string and makes it unparseable, silently losing
      // that turn's Context Explorer detail (confirmed via a live capture).
      // Raised here to 1MB, comfortably inside this server's own 25MB cap
      // on a whole OTLP batch.
      "export CLAUDE_CODE_OTEL_CONTENT_MAX_LENGTH=1048576",
    ].join("\n");
    const copilotOtelSnippet = [
      "export COPILOT_OTEL_ENABLED=true",
      "export OTEL_EXPORTER_OTLP_ENDPOINT=" + otlpUrl,
      'export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer ' + token + '"',
    ].join("\n");
    const copilotOtelOptin = "export COPILOT_OTEL_CAPTURE_CONTENT=true";

    return `
      <div class="card accent">
        <h3>Your connection</h3>
        <div style="margin-top: 0.9rem;">
          <div class="kv-row"><span class="kv-label">MCP server URL</span><span class="kv-value">` + mcpUrl + `</span></div>
          <div class="kv-row"><span class="kv-label">Your token</span><span class="kv-value">` + token + `</span></div>
        </div>
        <p class="card-hint" style="margin-top: 0.8rem;">Keep your token private: anyone with it can read and record data as you.</p>
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
        your own <code>~/.claude/settings.json</code>, the same file the manual snippets below
        would have you paste into by hand, applied for you instead. Your existing settings are
        backed up first and merged, never overwritten.</p>
        <p class="card-hint" style="color: var(--ok);">This is a local file write on your own
        machine only. Nothing here is sent anywhere except this server, which is also running
        on your machine right now.</p>
        <button class="copy" onclick="applyLocalConfig()" id="local-setup-btn">Apply to my Claude Code config</button>
        <div id="local-setup-result" style="margin-top: 0.7rem; font-size: 0.85rem;"></div>
      </div>
      ` : `
      <div id="install-card" class="card accent">
        <h3>Set up Claude Code</h3>
        <p class="card-hint">Once you run this, Claude Code streams your session data straight to
        this dashboard in the background. Nothing stored locally, nothing to keep running.</p>

        <p style="margin: 0.9rem 0 0.5rem; font-size: 0.87rem;">
          <em><strong>Please close any existing Claude Code sessions</strong></em> &mdash; terminal
          windows or editor integrations &mdash; before running this. Env vars only load once at
          process startup, so a session that's already open won't pick up the new config no
          matter how correct the file on disk now is.
        </p>

        <div class="tab-row" style="margin-bottom:0.55rem;">
          <button class="tab-btn active" id="os-tab-unix" onclick="setInstallOs('unix')">macOS / Linux</button>
          <button class="tab-btn" id="os-tab-win" onclick="setInstallOs('windows')">Windows (PowerShell)</button>
        </div>
        <pre id="install-cmd">fetching your install command&hellip;</pre>
        <div style="display:flex; gap:0.5rem;">
          <button class="copy" onclick="copyText('install-cmd')" id="install-copy-btn">Copy command</button>
          <button class="icon-btn" onclick="refreshInstallCommand()" id="install-refresh-btn" title="Get a fresh command (the old one expires after a few minutes)">&#8635; New command</button>
        </div>
        <p class="card-hint" style="margin-top: 0.6rem;">
          This command is single-use and expires in a few minutes. The code in the URL exchanges
          once for your real token, server-side, so the token itself never ends up sitting in your
          shell history. If it's gone stale, click "New command" for a fresh one.
        </p>

        <details style="margin-top: 0.9rem;">
          <summary>Not comfortable piping straight into a shell? Inspect it first</summary>
          <p class="card-hint">Same script either way; this just downloads it instead of piping
          it directly, so you (or <code>less</code>, or your editor) can read exactly what it's
          about to do before anything runs:</p>
          <pre id="install-cmd-inspect">fetching your install command&hellip;</pre>
          <button class="copy" onclick="copyText('install-cmd-inspect')">Copy command</button>
        </details>

        <div id="test-connection-card" style="margin-top: 1.1rem; padding-top: 1.1rem; border-top: 1px solid var(--border-soft);">
          <div class="setup-step-title">Test your connection</div>
          <div id="test-connection-result">
            <div class="setup-waiting"><span class="pulse"></span>
              <span>Waiting for your first prompt&hellip; run the command above, close and
              reopen Claude Code, then run one prompt. Check back here about 10 seconds after
              it finishes. That's how often telemetry exports.</span>
            </div>
          </div>
          <button class="icon-btn" onclick="checkConnection()" style="margin-top:0.6rem;" id="test-connection-btn">&#8635; Check now</button>
        </div>

      </div>
      `) + (isLocalHost ? `` : `

      <div class="card">
        <h3>Connect with Claude chat</h3>
        <p class="card-hint">Use ctxwindow's MCP tools from a claude.ai chat &mdash; ask
        "what did my last session cost?" or "show the token breakdown for session X" in any
        conversation, everywhere, with nothing written to a local file. This is separate from
        the Claude Code setup above; do both if you want the live dashboard <em>and</em>
        chat access.</p>
        <ol class="card-hint" style="padding-left: 1.2rem; margin: 0.8rem 0;">
          <li>Copy the MCP server URL: <code>` + mcpUrl + `</code>
            <button class="copy" onclick="copyText('connect-chat-url')" style="margin-left:0.4rem;">Copy</button>
            <span id="connect-chat-url" class="hidden">` + mcpUrl + `</span></li>
          <li>In claude.ai, open <strong>Settings &rarr; Connectors</strong> (or
            <strong>Customize &rarr; Connectors</strong>) and click <strong>Add custom connector</strong>.</li>
          <li>Paste the URL and click <strong>Add</strong>. Leave the OAuth Client ID / Secret
            fields blank &mdash; they aren't used here.</li>
          <li>claude.ai opens a Google sign-in for this server automatically. Sign in and the
            connector goes live. No token to copy anywhere.</li>
        </ol>
        <a class="copy" href="https://claude.ai/settings/connectors" target="_blank" rel="noopener" style="display:inline-block; text-decoration:none;">Open claude.ai Connectors &rarr;</a>
        <p class="card-hint" style="margin-top: 0.8rem;"><strong>Separate sign-in from Claude Code.</strong>
        The connector mints a token scoped to claude.ai only; your Claude Code CLI keeps its own
        token from the setup above. It also can't carry the OTLP telemetry env vars, so the
        dashboard won't auto-populate from chat use &mdash; run the "Claude Code (live telemetry)"
        snippet below for that.</p>
      </div>
      `) + `

      <details>
        <summary>Advanced: manual setup, or connecting a different client</summary>
        <div style="padding-left: 1.7rem;">
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
          <p class="card-hint">Bedrock-based agents: point them at the MCP server URL above with this header on every request:</p>
          <pre id="raw-header">` + rawHeader + `</pre>
          <button class="copy" onclick="copyText('raw-header')">Copy header</button>
          <p class="card-hint" style="margin-top: 0.9rem;">curl (debugging):</p>
          <pre id="curl-cmd">` + curlCmd + `</pre>
          <button class="copy" onclick="copyText('curl-cmd')">Copy curl</button>
        </div>
        <div class="tab-panel" data-panel="claude-otel">
          <p class="card-hint">Claude Code exports its own OpenTelemetry data natively. Point it at this server instead of
          (or alongside) the MCP connection to get live token/cost/tool-call telemetry with no extra tool calls needed:</p>
          <pre id="claude-otel-snippet">` + claudeOtelSnippet + `</pre>
          <button class="copy" onclick="copyText('claude-otel-snippet')">Copy snippet</button>
          <div class="otel-optin">
            <p class="card-hint"><strong>Optional, and powers the per-session Context Window Explorer.</strong> Without this,
            you still get token counts, cost, and tool-call telemetry from the snippet above. With it, Claude Code's own
            raw request/response bodies are captured, giving you the full block-by-block context breakdown, but per
            Claude Code's own docs, this is a materially bigger disclosure: "bodies include the entire conversation
            history." Add it only if you want that level of detail:</p>
            <pre id="claude-otel-optin">` + claudeOtelOptin + `</pre>
            <button class="copy" onclick="copyText('claude-otel-optin')">Copy opt-in line</button>
          </div>
        </div>
        <div class="tab-panel" data-panel="copilot-otel">
          <p class="card-hint">GitHub Copilot (VS Code) also exports OpenTelemetry natively. This covers Copilot Chat,
          which is VS Code's native AI surface, so no separate VS Code integration is needed:</p>
          <pre id="copilot-otel-snippet">` + copilotOtelSnippet + `</pre>
          <button class="copy" onclick="copyText('copilot-otel-snippet')">Copy snippet</button>
          <div class="otel-optin">
            <p class="card-hint"><strong>Optional, and powers the per-session Context Window Explorer.</strong> Without this,
            you still get token counts and tool-call telemetry from the snippet above. With it, Copilot exposes its own
            structured prompt/response content (` + "`gen_ai.input.messages`/`gen_ai.output.messages`" + `) for the full
            context breakdown, a bigger disclosure than token counts alone. Add it only if you want that level of detail:</p>
            <pre id="copilot-otel-optin">` + copilotOtelOptin + `</pre>
            <button class="copy" onclick="copyText('copilot-otel-optin')">Copy opt-in line</button>
          </div>
        </div>
        </div>
      </details>

      <div class="card accent">
        <h3>Live dashboard <span class="badge">ready</span></h3>
        <p class="card-hint">Your own sessions only. Every ` + "`record_session`" + ` call from your LLM/agent
        (recorded through the token above) shows up here within a few seconds, including the full
        Context Window Explorer breakdown. No separate app needed.</p>
        <button class="copy" onclick="goToDashboard()">Proceed to dashboard &rarr;</button>
      </div>

      <details>
        <summary>How do I record my own agent's sessions here, not just read?</summary>
        <p>Call the <code>record_session</code> MCP tool (or POST <code>/api/record-session</code>) with the same
        bearer token, so whatever you record is automatically attributed to you, the same way reads are scoped.
        See the package README's Auth section for the exact request shape.</p>
      </details>
      <details>
        <summary>Can this token be revoked?</summary>
        <p>Yes. The server owner can revoke your access at any time; you'd just sign in again here for a new one.
        Your already-recorded data isn't deleted, and stays visible only to you and the server owner.</p>
      </details>
    `;
  }

  function showConnectTab(name) {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.dataset.panel === name));
  }

  // --- Post-auth dashboard screen -------------------------------------
  // A separate full-width screen (not nested in the narrow sign-in/
  // config column) reached via "Proceed to dashboard" after a fresh
  // sign-in, or automatically on a returning visit with a stored token
  // (see rehydrateFromStorage), matching the approved dashboard mockup's
  // own topbar + wide layout rather than squeezing it into a card.
  function avatarInitial(email) {
    return (email || "?").trim()[0]?.toUpperCase() || "?";
  }

  function dashboardScreen(email, avatarLetter) {
    return `
      <div class="topbar">
        <div class="brand">
          <span class="brand-mark">&#9670;</span>
          <span style="font-weight:650; font-size:0.95rem;">CtxWindow</span>
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
            <button onclick="closeIdentityMenu(); refreshDashboard(currentToken, { force: true });">&#8635; Refresh now</button>
            <button onclick="openDevices();">&#128421; Devices &amp; sessions</button>
            <button class="danger" onclick="closeIdentityMenu(); signOut();">Sign out</button>
          </div>
        </div>
      </div>
      <div id="dash-root"><p class="dash-empty">Loading…</p></div>
      <div id="settings-root" class="hidden"></div>
    `;
  }

  function goToDashboard() {
    document.querySelector(".narrow-page").classList.add("hidden");
    document.getElementById("landing-topbar")?.classList.add("hidden");
    const screen = document.getElementById("dashboard-screen");
    screen.innerHTML = dashboardScreen(currentEmail, avatarInitial(currentEmail));
    screen.classList.remove("hidden");
    mountDashboard(currentToken);
  }

  function backToConnect() {
    if (dashboardTimer) clearInterval(dashboardTimer);
    document.getElementById("dashboard-screen").classList.add("hidden");
    document.querySelector(".narrow-page").classList.remove("hidden");
    document.getElementById("landing-topbar")?.classList.remove("hidden");
  }

  function toggleIdentityMenu(evt) {
    evt.stopPropagation();
    const dd = document.getElementById("identity-dropdown");
    const opening = dd.classList.contains("hidden");
    dd.classList.toggle("hidden", !opening);
    evt.currentTarget.setAttribute("aria-expanded", opening ? "true" : "false");
  }

  function closeIdentityMenu() {
    const dd = document.getElementById("identity-dropdown");
    if (dd) dd.classList.add("hidden");
    const trigger = document.querySelector(".identity-trigger");
    if (trigger) trigger.setAttribute("aria-expanded", "false");
  }

  // Closes the identity dropdown on any click outside it. Cheap to
  // register once at load rather than per dashboardScreen() render,
  // since #identity-dropdown only exists (and only needs closing)
  // while the dashboard screen is mounted; closeIdentityMenu() itself
  // already no-ops safely when it doesn't.
  document.addEventListener("click", closeIdentityMenu);

  function copyCurrentToken() {
    if (currentToken) navigator.clipboard.writeText(currentToken);
  }

  // --- Devices & sessions -------------------------------------------------
  // Lists every per-device sign-in token and connector session for this
  // account (GET /auth/devices), each with a Revoke button (POST
  // /auth/revoke-device with its token_id). The row for the token this
  // browser is holding is marked "This device"; revoking it signs this
  // browser out. Rendered as a lightweight overlay appended to the
  // dashboard screen, dismissed on backdrop click or Esc.
  function devicesOverlayHtml() {
    return `
      <div class="devices-backdrop" id="devices-backdrop" onclick="closeDevices(event)">
        <div class="devices-modal" role="dialog" aria-label="Devices and sessions" onclick="event.stopPropagation()">
          <div class="devices-modal-head">
            <span>Devices &amp; sessions</span>
            <button class="devices-close" onclick="closeDevices()" aria-label="Close">&times;</button>
          </div>
          <p class="devices-hint">Each device you sign in from, and each connected app, holds its own
          token. Revoke one to disconnect just that device &mdash; the others stay signed in.</p>
          <div id="devices-list"><p class="dash-empty">Loading&hellip;</p></div>
        </div>
      </div>
    `;
  }

  async function openDevices() {
    closeIdentityMenu();
    const screen = document.getElementById("dashboard-screen");
    if (!document.getElementById("devices-backdrop")) {
      screen.insertAdjacentHTML("beforeend", devicesOverlayHtml());
      document.addEventListener("keydown", devicesEscHandler);
    }
    await loadDevices();
  }

  function devicesEscHandler(e) { if (e.key === "Escape") closeDevices(); }

  function closeDevices(evt) {
    if (evt && evt.target && evt.target.id !== "devices-backdrop" && evt.type === "click") return;
    const bd = document.getElementById("devices-backdrop");
    if (bd) bd.remove();
    document.removeEventListener("keydown", devicesEscHandler);
  }

  async function loadDevices() {
    const list = document.getElementById("devices-list");
    if (!list) return;
    try {
      const data = await apiGet(currentToken, "/auth/devices");
      const devices = (data && data.devices) || [];
      if (!devices.length) {
        list.innerHTML = '<p class="dash-empty">No other devices or sessions.</p>';
        return;
      }
      list.innerHTML = devices.map(function (d) {
        const seen = d.last_seen_at ? timeAgo(d.last_seen_at) : "not seen yet";
        const added = d.created_at ? timeAgo(d.created_at) : "";
        const current = d.is_current
          ? '<span class="device-current">This device</span>' : "";
        const kind = d.kind === "connector" ? "Connector session" : "Sign-in";
        const revoke = d.token_id
          ? '<button class="icon-btn device-revoke" onclick="revokeDevice(\'' + escapeHtml(d.token_id) + '\', ' + (d.is_current ? 'true' : 'false') + ')">Revoke</button>'
          : '<span class="device-noid">no id</span>';
        return '<div class="device-row">'
          + '<div class="device-row-main">'
          + '<div class="device-label">' + escapeHtml(d.label || "Unknown device") + ' ' + current + '</div>'
          + '<div class="device-meta">' + kind + ' &middot; last seen ' + escapeHtml(seen)
          + (added ? ' &middot; added ' + escapeHtml(added) : '') + '</div>'
          + '</div>' + revoke + '</div>';
      }).join("");
    } catch (err) {
      list.innerHTML = '<p class="dash-error">Could not load devices: ' + escapeHtml(err.message) + '</p>';
    }
  }

  async function revokeDevice(tokenId, isCurrent) {
    if (isCurrent && !confirm("This is the device you're using now. Revoking it signs you out here. Continue?")) return;
    try {
      const res = await fetch("/auth/revoke-device", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + currentToken },
        body: JSON.stringify({ token_id: tokenId }),
      });
      if (!res.ok) throw new Error("HTTP " + res.status);
      if (isCurrent) { closeDevices(); signOut(); return; }
      await loadDevices();
    } catch (err) {
      const list = document.getElementById("devices-list");
      if (list) list.insertAdjacentHTML("afterbegin",
        '<p class="dash-error">Revoke failed: ' + escapeHtml(err.message) + '</p>');
    }
  }

  function copyText(id) {
    navigator.clipboard.writeText(document.getElementById(id).textContent);
  }

  async function applyLocalConfig() {
    const btn = document.getElementById("local-setup-btn");
    const result = document.getElementById("local-setup-result");
    btn.disabled = true;
    btn.textContent = "Applying…";
    try {
      const res = await fetch("/setup/apply-local-config", {
        method: "POST",
        headers: { Authorization: "Bearer " + currentToken },
      });
      const data = await res.json();
      if (res.ok && data.ok) {
        result.innerHTML = '<span style="color:var(--ok);">&check; Done. Wrote to <code>' + escapeHtml(data.path) + '</code>'
          + (data.backed_up_to ? ' (previous version backed up to <code>' + escapeHtml(data.backed_up_to) + '</code>)' : '')
          + '. Restart any running Claude Code sessions to pick it up.</span>';
        btn.textContent = "Applied";
      } else {
        result.innerHTML = '<span style="color:var(--err);">' + escapeHtml(data.error || "Something went wrong.") + '</span>';
        btn.disabled = false;
        btn.textContent = "Apply to my Claude Code config";
      }
    } catch (err) {
      result.innerHTML = '<span style="color:var(--err);">' + escapeHtml(err.message) + '</span>';
      btn.disabled = false;
      btn.textContent = "Apply to my Claude Code config";
    }
  }

  // Mints a fresh short-lived install code (see POST
  // /setup/issue-install-code) and renders both the piped one-liner and
  // the inspect-first (download, read, then run) variant of the exact
  // same command. The code is single-use and expires in a few minutes,
  // so this re-mints on every call rather than caching, so "New command"
  // (and page reload) always gets a live one.
  // Auto-detect once; the toggle above overrides. navigator.platform is
  // deprecated but still the most reliable Windows signal in every
  // current browser; the UA regex is the fallback in the same test.
  let installOs = /win/i.test((navigator.platform || "") + " " + (navigator.userAgent || "")) ? "windows" : "unix";

  function setInstallOs(os) {
    installOs = os;
    const u = document.getElementById("os-tab-unix");
    const w = document.getElementById("os-tab-win");
    if (u) u.classList.toggle("active", os === "unix");
    if (w) w.classList.toggle("active", os === "windows");
    refreshInstallCommand();
  }

  async function refreshInstallCommand() {
    const cmdEl = document.getElementById("install-cmd");
    const inspectEl = document.getElementById("install-cmd-inspect");
    const btn = document.getElementById("install-refresh-btn");
    if (!cmdEl) return;  // localhost path renders a different card with no install-cmd element
    if (!currentToken) {
      // Nothing to authenticate with yet (still rehydrating, or signed
      // out). Leave the placeholder text; whoever set currentToken will
      // call this again.
      if (btn) { btn.disabled = false; btn.classList.remove("spinning"); }
      return;
    }
    if (btn) { btn.disabled = true; btn.classList.add("spinning"); }
    try {
      const res = await fetch("/setup/issue-install-code", {
        method: "POST",
        headers: { Authorization: "Bearer " + currentToken },
      });
      // A dead token here means the same thing it means for any other
      // /api/ call: this browser's stored token was revoked server-side.
      // Sign out rather than printing a raw "unauthorized" string into
      // the setup card (which looked like a bug in the installer itself).
      if (res.status === 401 || res.status === 403) {
        signOut();
        return;
      }
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
      const codeParam = "t=" + encodeURIComponent(data.code);
      if (installOs === "windows") {
        const url = canonicalOrigin + "/setup/install?os=windows&" + codeParam;
        cmdEl.textContent = 'irm "' + url + '" | iex';
        if (inspectEl) {
          inspectEl.textContent = 'irm "' + url + '" -OutFile install.ps1\n'
            + 'Get-Content install.ps1        # read exactly what it will do\n'
            + '.\\install.ps1';
        }
      } else {
        const url = canonicalOrigin + "/setup/install?" + codeParam;
        cmdEl.textContent = "curl -fsSL " + url + " | sh";
        if (inspectEl) {
          inspectEl.textContent = "curl -fsSL " + url + " -o install.sh\nless install.sh"
            + "        # read exactly what it's about to do\nsh install.sh";
        }
      }
    } catch (err) {
      // Transient failure (offline, 5xx) -- token's still good, so keep
      // the retryable message rather than signing out.
      cmdEl.textContent = "Couldn't fetch an install command: " + err.message + ". Click \"New command\" to retry.";
    } finally {
      if (btn) { btn.disabled = false; btn.classList.remove("spinning"); }
    }
  }

  // "Test your connection": a single on-demand check against
  // GET /otlp/debug (owner-scoped, see mcp_server/otlp/__init__.py),
  // never a polling loop: Claude Code only exports telemetry on actual
  // use, so there's no honest way to fake a heartbeat here. Three
  // states: still waiting (nothing accepted and nothing skipped yet),
  // connected (at least one claude_code payload accepted), or received-
  // but-unrecognized (something landed in recent_skipped, i.e. the vendor-
  // detection miss this project hit once before, see otlp/__init__.py's
  // detect_vendor).
  async function checkConnection() {
    const resultEl = document.getElementById("test-connection-result");
    const btn = document.getElementById("test-connection-btn");
    if (!resultEl) return;
    btn.disabled = true;
    try {
      const res = await fetch("/otlp/debug", { headers: { Authorization: "Bearer " + currentToken } });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));

      if (data.counts.claude_code > 0) {
        const lastAt = data.last_accepted_at.claude_code;
        const when = lastAt ? new Date(lastAt * 1000).toLocaleString() : "just now";
        resultEl.innerHTML = '<div class="setup-waiting" style="background:var(--accent-dim); border-color: color-mix(in srgb, var(--accent) 40%, transparent);">'
          + '<span style="color:var(--accent);">&check;</span>'
          + '<span><strong>Connected as ' + currentEmail + '</strong>, first session seen ' + when + '.</span></div>';
      } else if (data.counts.skipped > 0 || (data.recent_skipped && data.recent_skipped.length > 0)) {
        resultEl.innerHTML = '<div class="setup-waiting">'
          + '<span style="color:var(--err);">&#9888;</span>'
          + '<span>We\'re receiving data from your machine but can\'t identify it as Claude Code yet. '
          + 're-run the install command above, then close and reopen Claude Code before your next prompt.</span></div>';
      } else {
        resultEl.innerHTML = '<div class="setup-waiting"><span class="pulse"></span>'
          + '<span>Waiting for your first prompt&hellip; run the command above, close and '
          + 'reopen Claude Code, then run one prompt. Check back here about 10 seconds after '
          + 'it finishes. That\'s how often telemetry exports.</span></div>';
      }
    } catch (err) {
      resultEl.innerHTML = '<span style="color:var(--err);">' + escapeHtml(err.message) + '</span>';
    } finally {
      btn.disabled = false;
    }
  }

  // --- Live dashboard ------------------------------------------------
  // Renders each authenticated caller's own sessions right on this page,
  // via the same /api/* routes and bearer token an LLM/agent uses, with no
  // separate client needed to actually see what got recorded. Every
  // read here is already owner-scoped server-side (see
  // MultiTokenAuthMiddleware + metrics/store.py's owner filtering), so
  // this page can never show another user's data even if it tried to.
  //
  // KPI strip / range filter: fetches /api/sessions?limit=N (see
  // DASHBOARD_SESSION_LIMIT) ONCE and
  // aggregates client-side (same "personal-project scale" assumption
  // already used elsewhere in this codebase) instead of adding new
  // backend aggregate endpoints. Every tile (sessions, tokens, spend,
  // cache hit rate, tool error rate, context alerts) is computed from
  // that single bulk list response; none require a per-session detail
  // fetch.

  const CATEGORY_COLORS = {
    system: "var(--cat-system)", tools: "var(--cat-tools)", user: "var(--cat-user)",
    injected: "var(--cat-injected)", command: "var(--cat-command)",
    reasoning: "var(--cat-reasoning)", thinking: "var(--cat-reasoning)",
    tool_call: "var(--cat-toolcall)", tool_result: "var(--cat-toolresult)", answer: "var(--cat-answer)",
  };
  // Order + display name for the Context Explorer legend / filter. Every
  // category the OTLP mappers can emit is here; `thinking` folds into
  // `reasoning` (same colour, same meaning -- extended-thinking content
  // that Claude Code redacts before export).
  const CTX_CATEGORIES = [
    ["system", "system"], ["tools", "tools"], ["user", "user"],
    ["injected", "injected"], ["command", "command"], ["reasoning", "reasoning"],
    ["tool_call", "tool call"], ["tool_result", "tool result"], ["answer", "answer"],
  ];
  const CTX_FILTER_KEY = "mci_ctx_hidden_cats";
  // Advanced-filter state (search text is deliberately NOT persisted --
  // a stale query on reload is noise; everything else is).
  const CTX_ADV_KEY = "mci_ctx_adv_filter";
  function loadHiddenCats() {
    try {
      const raw = localStorage.getItem(CTX_FILTER_KEY);
      return raw ? new Set(JSON.parse(raw)) : new Set();
    } catch (e) { return new Set(); }
  }
  function saveHiddenCats(set) {
    try { localStorage.setItem(CTX_FILTER_KEY, JSON.stringify([...set])); } catch (e) {}
  }
  function loadAdvFilter() {
    const def = { minTokens: 0, turnFrom: null, turnTo: null, errorsOnly: false, hideRedacted: false, open: false };
    try {
      const raw = localStorage.getItem(CTX_ADV_KEY);
      return raw ? Object.assign(def, JSON.parse(raw)) : def;
    } catch (e) { return def; }
  }
  function saveAdvFilter() {
    try {
      const { minTokens, turnFrom, turnTo, errorsOnly, hideRedacted, open } = ctxAdv;
      localStorage.setItem(CTX_ADV_KEY, JSON.stringify({ minTokens, turnFrom, turnTo, errorsOnly, hideRedacted, open }));
    } catch (e) {}
  }
  let ctxHiddenCats = loadHiddenCats();
  let ctxAdv = loadAdvFilter();
  let ctxSearch = "";  // never persisted
  let ctxTimeline = [];
  const ctxCollapsedTurns = new Set();

  const SRC_BADGE = {
    claude_code: {cls: "cc", label: "CC"},
    copilot: {cls: "gh", label: "GH"},
    bedrock_agent: {cls: "bd", label: "BD"},
  };

  async function apiGet(token, path) {
    const res = await fetch(path, { headers: { Authorization: "Bearer " + token } });
    if (!res.ok) throw new Error("HTTP " + res.status);
    return res.json();
  }

  function fmtCost(v) { return "$" + (v || 0).toFixed(4); }
  function fmtTokens(n) {
    n = n || 0;
    if (n >= 1000000) return (n / 1000000).toFixed(1) + "M";
    if (n >= 1000) return (n / 1000).toFixed(1) + "k";
    return String(n);
  }
  function timeAgo(ts) {
    if (!ts && ts !== 0) return "n/a";
    // Sessions carry `timestamp` as epoch seconds; agent-trace entries
    // carry it as an ISO-8601 string (e.g. "2026-08-30T11:22:33Z").
    // Accept both, plus epoch milliseconds, so the Tool calls tab stops
    // rendering "NaNd ago".
    let epochSecs;
    if (typeof ts === "number") {
      epochSecs = ts > 1e11 ? ts / 1000 : ts;  // >~year 5138 in secs => it's ms
    } else {
      const parsed = Date.parse(ts);
      if (isNaN(parsed)) return "n/a";
      epochSecs = parsed / 1000;
    }
    const secs = Math.max(0, Date.now() / 1000 - epochSecs);
    if (secs < 60) return "just now";
    if (secs < 3600) return Math.floor(secs / 60) + "m ago";
    if (secs < 86400) return Math.floor(secs / 3600) + "h ago";
    return Math.floor(secs / 86400) + "d ago";
  }

  // Auto-refresh cadence. Each poll is a GET /api/sessions?limit=N, which
  // on the Firestore backend is a query plus a billed read per returned
  // session doc. 60s is still live enough for a human watching a session
  // list, and the poll is skipped entirely while the tab is backgrounded
  // (see refreshDashboard).
  const DASHBOARD_POLL_MS = 60000;
  // Upper bound on sessions pulled per poll. The list, KPI/quota/insight
  // strips are all derived client-side from this one payload, so it must
  // cover the visible range.
  const DASHBOARD_SESSION_LIMIT = 200;

  let dashboardTimer = null;
  let dashboardVisibilityHooked = false;
  let dashboardSelected = null;
  let dashboardRange = "7d"; // "today" | "7d" | "30d" | "all"
  let dashboardSourceFilter = "all"; // "all" | "claude_code" | "copilot" | "bedrock_agent"
  let dashboardSessions = []; // full bulk list (up to DASHBOARD_SESSION_LIMIT), unfiltered
  let dashboardAutoRefresh = true;
  // Whether this signed-in account is on the DEV_MODE_SUBS allowlist
  // (see GET /api/dev-mode-status), checked once per mount, since it
  // can't change mid-session. Everyone else never sees the toggle at
  // all, not a shown-but-disabled one.
  let dashboardIsDevMode = false;
  let dashboardShowTestSessions = false;

  const RANGE_SECONDS = {today: 86400, "7d": 7 * 86400, "30d": 30 * 86400, all: null};
  const SOURCE_LABELS = {claude_code: "Claude Code", copilot: "Copilot", bedrock_agent: "Bedrock agent"};

  // Mirrors mci_common.config.CONTEXT_WINDOW_TOKENS, duplicated here
  // rather than plumbed through the API response because every session
  // in the list already carries total_tokens, and pulling this one
  // constant server-side into the list endpoint isn't worth a new
  // response field. Keep in sync if that constant ever changes.
  const CONTEXT_WINDOW_TOKENS = 200000;

  function ctxPressure(totalTokens) {
    const pct = Math.min(100, ((totalTokens || 0) / CONTEXT_WINDOW_TOKENS) * 100);
    const level = pct >= 95 ? "err" : pct >= 80 ? "warn" : "ok";
    return {pct, level};
  }

  function sessionsInRange() {
    const secs = RANGE_SECONDS[dashboardRange];
    const cutoff = secs === null ? null : Date.now() / 1000 - secs;
    return dashboardSessions.filter((s) => {
      if (cutoff !== null && (s.timestamp || 0) < cutoff) return false;
      if (dashboardSourceFilter === "other") {
        if (SOURCE_LABELS[s.source]) return false;
      } else if (dashboardSourceFilter !== "all" && s.source !== dashboardSourceFilter) {
        return false;
      }
      return true;
    });
  }

  function renderKpiStrip() {
    const inRange = sessionsInRange();
    const tokens = inRange.reduce((sum, s) => sum + (s.total_tokens || 0), 0);
    const spend = inRange.reduce((sum, s) => sum + (s.estimated_cost || 0), 0);
    const alertCount = inRange.filter((s) => ctxPressure(s.total_tokens).level !== "ok").length;
    const totalCacheRead = inRange.reduce((sum, s) => sum + (s.cache_read_tokens || 0), 0);
    const totalFreshInput = inRange.reduce((sum, s) => sum + (s.fresh_input_tokens || 0), 0);
    const cacheDenom = totalCacheRead + totalFreshInput;
    const cacheHitRate = cacheDenom ? Math.round((totalCacheRead / cacheDenom) * 100) + "%" : "n/a";
    const totalToolCalls = inRange.reduce((sum, s) => sum + (s.tool_call_total || 0), 0);
    const totalToolErrors = inRange.reduce((sum, s) => sum + (s.tool_call_errors || 0), 0);
    const toolErrorRate = totalToolCalls ? Math.round((totalToolErrors / totalToolCalls) * 100) + "%" : "n/a";
    return `
      <div class="kpi"><span class="kpi-label">Sessions</span><span class="kpi-value">` + inRange.length + `</span></div>
      <div class="kpi"><span class="kpi-label">Tokens</span><span class="kpi-value">` + fmtTokens(tokens) + `</span></div>
      <div class="kpi"><span class="kpi-label">Spend</span><span class="kpi-value">` + fmtCost(spend) + `</span></div>
      <div class="kpi"><span class="kpi-label">Cache hit rate</span><span class="kpi-value">` + cacheHitRate + `</span></div>
      <div class="kpi"><span class="kpi-label">Tool error rate</span><span class="kpi-value">` + toolErrorRate + `</span></div>
      <div class="kpi"><span class="kpi-label">Context alerts</span><span class="kpi-value` + (alertCount ? " accent-warn" : "") + `">` + alertCount + `<small> &ge;80% window</small></span></div>
    `;
  }

  function renderRangeRow() {
    const opts = [["today", "Today"], ["7d", "7d"], ["30d", "30d"], ["all", "All time"]];
    return `
      <div class="filter-row" style="padding: 0;">
        ` + opts.map(([key, label]) =>
          '<span class="chip' + (dashboardRange === key ? ' active' : '') + '" onclick="setDashboardRange(\'' + key + '\')">' + label + '</span>'
        ).join("") + `
      </div>`;
  }

  function renderSourceFilterRow() {
    const present = new Set(dashboardSessions.map((s) => s.source));
    const hasOther = [...present].some((s) => !SOURCE_LABELS[s]);
    const opts = [["all", "All sources"]].concat(
      Object.keys(SOURCE_LABELS).filter((k) => present.has(k)).map((k) => [k, SOURCE_LABELS[k]])
    );
    if (hasOther) opts.push(["other", "Other"]);
    if (opts.length <= 1) return "";
    return `
      <div class="filter-row" style="padding: 0;">
        ` + opts.map(([key, label]) =>
          '<span class="chip' + (dashboardSourceFilter === key ? ' active' : '') + '" onclick="setSourceFilter(\'' + key + '\')">' + label + '</span>'
        ).join("") + `
      </div>`;
  }

  function renderInsightStrip() {
    const inRange = sessionsInRange();
    if (inRange.length < 2) return "";
    const cards = [];

    const pressureCount = inRange.filter((s) => ctxPressure(s.total_tokens).level !== "ok").length;
    cards.push(pressureCount
      ? '<div class="insight-card"><div class="i-label">Context pressure</div><div class="i-body"><strong>' + pressureCount + ' of ' + inRange.length + '</strong> sessions this period crossed 80% of the context window.</div></div>'
      : '<div class="insight-card"><div class="i-label">Context pressure</div><div class="i-body">No sessions this period crossed 80% of the context window.</div></div>');

    const bySource = {};
    inRange.forEach((s) => { bySource[s.source] = (bySource[s.source] || 0) + 1; });
    const sources = Object.keys(bySource);
    if (sources.length) {
      const topSource = sources.reduce((a, b) => (bySource[a] >= bySource[b] ? a : b));
      cards.push('<div class="insight-card"><div class="i-label">Busiest source</div><div class="i-body"><strong>' + (SOURCE_LABELS[topSource] || topSource) + '</strong> (' + bySource[topSource] + ' of ' + inRange.length + ' sessions this period).</div></div>');
    }

    const spend = inRange.reduce((sum, s) => sum + (s.estimated_cost || 0), 0);
    cards.push('<div class="insight-card"><div class="i-label">Spend this period</div><div class="i-body"><strong>' + fmtCost(spend) + '</strong> across ' + inRange.length + ' sessions (' + fmtCost(spend / inRange.length) + ' avg).</div></div>');

    return '<div class="insight-strip">' + cards.join("") + '</div>';
  }

  function renderQuotaStrip() {
    // Neither window is wired to a real data source yet; see
    // docs/internal/OTLP_INTEGRATION_PLAN.md's "5-hour / 7-day usage-window
    // percentage" verdict (not achievable via any supported path right
    // now). Kept visually complete per that doc's framing, with the
    // pending-source badge baked into .quota-card.pending and no
    // fabricated percentage or fill width.
    const card = (label) => `
      <div class="quota-card pending">
        <div class="quota-top"><span class="q-label">` + label + `</span><span class="q-pct">&mdash;</span></div>
        <div class="quota-track"><div class="quota-fill" style="width:0%;"></div></div>
        <div class="quota-sub">Not yet wired to a data source; see project plan.</div>
      </div>`;
    return card("5h usage window") + card("7d usage window");
  }

  function renderSessionRow(s) {
    const prompt = (s.prompt || "(no prompt)").slice(0, 60);
    const active = s.session_id === dashboardSelected ? " active" : "";
    const badge = SRC_BADGE[s.source] || {cls: "other", label: "Other"};
    const pressure = ctxPressure(s.total_tokens);
    const dotTitle = pressure.level === "ok" ? "Context window usage: " + Math.round(pressure.pct) + "%"
      : "Context window usage: " + Math.round(pressure.pct) + "%, approaching the limit";
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
  }

  function renderSessionListPanel() {
    const inRange = sessionsInRange();
    const body = !inRange.length
      ? '<p class="dash-empty">No sessions in view yet. Signing in here only grants query access to data recorded elsewhere, such as Claude Code/Copilot telemetry or an agent calling record_session. It does not start recording this chat\'s own activity. Set up telemetry (see the connect page) or call record_session and this list fills in automatically, no page refresh needed.</p>'
      : inRange.map(renderSessionRow).join("");
    return `
      <div class="panel">
        <div class="panel-head">
          <span class="panel-title">Sessions</span>
          <div class="refresh-controls">` + (dashboardIsDevMode ? `
            <span class="chip` + (dashboardShowTestSessions ? "" : " auto-off") + `" onclick="toggleTestSessions()" title="Show/hide api_tests probe sessions (dev-mode only, not visible to other accounts)">` + (dashboardShowTestSessions ? "Test sessions: shown" : "Test sessions: hidden") + `</span>
          ` : ``) + `
            <span class="chip` + (dashboardAutoRefresh ? "" : " auto-off") + `" onclick="toggleAutoRefresh()" title="Toggle automatic refresh">` + (dashboardAutoRefresh ? "Auto-refresh on" : "Auto-refresh off") + `</span>
            <button class="icon-btn" id="manual-refresh-btn" onclick="manualRefresh()" title="Refresh now">&#8635;</button>
          </div>
        </div>
        <div id="source-filter-row">` + renderSourceFilterRow() + `</div>
        <div class="session-list" id="session-list">` + body + `</div>
      </div>`;
  }

  // The single predicate every Context Explorer render path filters
  // through: category toggle + advanced filters (search text, min-token
  // threshold, turn range, errors-only, hide-redacted). All render
  // functions (bar, legend counts, summary, block list) use this so the
  // strip, the counts and the rows can never disagree.
  function ctxBlockPasses(b, opts) {
    // opts.ignoreCats: skip the category-toggle check (the legend uses
    // this to count how many blocks a hidden swatch would bring back).
    if (!(opts && opts.ignoreCats) && ctxHiddenCats.has(b.category)) return false;
    if (ctxAdv.minTokens > 0 && (b.token_estimate || 0) < ctxAdv.minTokens) return false;
    const t = (b.turn_n == null) ? -1 : b.turn_n;
    if (ctxAdv.turnFrom != null && t < ctxAdv.turnFrom) return false;
    if (ctxAdv.turnTo != null && t > ctxAdv.turnTo) return false;
    if (ctxAdv.errorsOnly && b.status !== "error") return false;
    if (ctxAdv.hideRedacted && b.status === "redacted") return false;
    if (ctxSearch) {
      const hay = ((b.content || "") + " " + (b.label || "") + " " + (b.category || "")).toLowerCase();
      if (hay.indexOf(ctxSearch) === -1) return false;
    }
    return true;
  }
  // Backwards-compatible alias -- some call sites still read ctxVisible.
  function ctxVisible(b) { return ctxBlockPasses(b); }
  function ctxAdvActive() {
    return ctxAdv.minTokens > 0 || ctxAdv.turnFrom != null || ctxAdv.turnTo != null
      || ctxAdv.errorsOnly || ctxAdv.hideRedacted || !!ctxSearch;
  }

  function renderContextBar(timeline) {
    // Bar always reflects the FILTERED view: hidden categories contribute
    // no segment, and widths are re-normalised over what's shown, so the
    // strip and the list agree.
    const shown = timeline.filter(ctxBlockPasses);
    const total = shown.reduce((s, b) => s + (b.token_estimate || 0), 0);
    if (!total) return '<div style="width:100%; background:var(--border-soft);"></div>';
    return shown.map((b) => {
      const color = CATEGORY_COLORS[b.category] || "var(--cat-system)";
      const pct = b.token_estimate / total * 100;
      return '<div style="width:' + pct + '%; background:' + color + ';"></div>';
    }).join("");
  }

  function escapeHtml(s) {
    const div = document.createElement("div");
    div.textContent = s == null ? "" : String(s);
    return div.innerHTML;
  }

  // escapeHtml (textContent -> innerHTML) escapes & < > but NOT quotes, so
  // it is unsafe for an unquoted-by-the-browser value that lands inside a
  // value="..." attribute: a " in the string closes the attribute early
  // and the rest becomes markup. Use this for every ="..." interpolation
  // of user-controlled text (currently the Context Explorer search box).
  function escapeAttr(s) {
    return escapeHtml(s).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function toggleBlockDetail(idx) {
    const row = document.getElementById("block-row-" + idx);
    const detail = document.getElementById("block-detail-" + idx);
    if (!row || !detail) return;
    const opening = detail.classList.contains("hidden");
    detail.classList.toggle("hidden", !opening);
    row.classList.toggle("expanded", opening);
  }

  // Wraps every case-insensitive occurrence of `q` in `text` with a
  // <mark>, on already-escaped HTML. q is lowercased search text.
  // Bail out if q holds an HTML metacharacter: it would match inside an
  // entity in escapedText (searching "&" hits the "&" of "&amp;") and
  // split it. The row still shows -- ctxBlockPasses matched raw content.
  function highlightMatch(escapedText, q) {
    if (!q || /[<>&"']/.test(q)) return escapedText;
    const lower = escapedText.toLowerCase();
    let out = "", from = 0, at;
    while ((at = lower.indexOf(q, from)) !== -1) {
      out += escapedText.slice(from, at) + '<mark>' + escapedText.slice(at, at + q.length) + '</mark>';
      from = at + q.length;
    }
    return out + escapedText.slice(from);
  }

  function renderContextBlockRow(b, idx) {
    const color = CATEGORY_COLORS[b.category] || "var(--cat-system)";
    const rawLabel = b.status === "redacted"
      ? escapeHtml(b.label || b.category) + ' (redacted)'
      : escapeHtml(b.label || b.category);
    const label = b.status === "redacted"
      ? '<span class="redacted">' + highlightMatch(rawLabel, ctxSearch) + '</span>'
      : highlightMatch(rawLabel, ctxSearch);
    const hasContent = typeof b.content === "string" && b.content.length > 0;
    const detail = hasContent
      ? '<div class="block-detail hidden" id="block-detail-' + idx + '">' + highlightMatch(escapeHtml(b.content), ctxSearch) + '</div>'
      : '<div class="block-detail unavailable hidden" id="block-detail-' + idx + '">' +
        (b.status === "redacted"
          ? "Content is redacted by the client itself before export, so it is not available here either."
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
  }

  // Collapse a run of 3+ consecutive same-category blocks (after
  // filtering) into a single summary line; shorter runs render in full.
  function renderBlockSequence(blocks) {
    let html = "";
    let i = 0;
    while (i < blocks.length) {
      let j = i;
      while (j < blocks.length && blocks[j].b.category === blocks[i].b.category) j++;
      const run = blocks.slice(i, j);
      if (run.length >= 3) {
        const cat = run[0].b.category;
        const toks = run.reduce((s, x) => s + (x.b.token_estimate || 0), 0);
        const name = (CTX_CATEGORIES.find((c) => c[0] === cat) || [cat, cat])[1];
        html += '<div class="block-run">' + run.length + ' &times; ' + escapeHtml(name)
          + ' &middot; ' + fmtTokens(toks) + ' tok</div>';
        html += run.map((x) => renderContextBlockRow(x.b, x.idx)).join("");
      } else {
        html += run.map((x) => renderContextBlockRow(x.b, x.idx)).join("");
      }
      i = j;
    }
    return html;
  }

  function renderCtxLegend(timeline) {
    // Per-category counts reflect the OTHER advanced filters (so a
    // swatch shows how many blocks it would add back), but not its own
    // category toggle.
    const counts = {};
    timeline.forEach((b) => {
      if (ctxBlockPasses(b, { ignoreCats: true })) {
        counts[b.category] = (counts[b.category] || 0) + 1;
      }
    });
    const present = CTX_CATEGORIES.filter(([key]) => counts[key]);
    const swatches = present.map(([key, name]) => {
      const off = ctxHiddenCats.has(key) ? " off" : "";
      return '<span class="' + off.trim() + '" onclick="toggleCtxCategory(\'' + key + '\')" '
        + 'title="' + counts[key] + ' block(s) &mdash; click to ' + (off ? 'show' : 'hide') + '">'
        + '<i style="background:' + (CATEGORY_COLORS[key] || 'var(--cat-system)') + ';"></i>' + name
        + ' <span class="legend-n">' + counts[key] + '</span></span>';
    }).join("");
    return '<div class="ctx-legend">' + swatches
      + '<span class="legend-actions">'
      + '<button onclick="ctxFilterAll()">All</button>'
      + '<button onclick="ctxFilterNone()">None</button>'
      + '</span></div>';
  }

  function renderCtxFilterSummary(timeline) {
    const shown = timeline.filter(ctxBlockPasses);
    if (shown.length === timeline.length) return "";
    const total = timeline.reduce((s, b) => s + (b.token_estimate || 0), 0) || 1;
    const shownTok = shown.reduce((s, b) => s + (b.token_estimate || 0), 0);
    const pct = (shownTok / total * 100).toFixed(1);
    return '<div class="ctx-filter-summary">Showing ' + shown.length + ' of ' + timeline.length
      + ' blocks &middot; ' + pct + '% of tokens'
      + (ctxAdvActive() ? ' <button class="ctx-clear-adv" onclick="ctxClearAdvanced()">Clear filters</button>' : '')
      + '</div>';
  }

  function renderCtxBlocks(timeline) {
    // Group the FILTERED blocks by turn_n into collapsible sections.
    const groups = new Map();
    timeline.forEach((b, idx) => {
      if (!ctxBlockPasses(b)) return;
      const t = (b.turn_n == null) ? -1 : b.turn_n;
      if (!groups.has(t)) groups.set(t, []);
      groups.get(t).push({ b, idx });
    });
    if (!groups.size) {
      // Distinguish the two ways the list empties: every category toggled
      // off (fix: "All"), vs. the advanced filters (search / min-tokens /
      // turn range / errors-only) excluding everything.
      const anyPassIgnoringCats = timeline.some((b) => ctxBlockPasses(b, { ignoreCats: true }));
      if (anyPassIgnoringCats && ctxHiddenCats.size) {
        return '<div class="ctx-filter-summary">Every category is hidden &mdash; use '
          + '<button class="ctx-clear-adv" onclick="ctxFilterAll();">All</button> to show blocks again.</div>';
      }
      return '<div class="ctx-filter-summary">No blocks match the current filters. '
        + '<button class="ctx-clear-adv" onclick="ctxClearAdvanced(); ctxFilterAll();">Clear all filters</button></div>';
    }
    const turnKeys = [...groups.keys()].sort((a, b) => a - b);
    const lastTurn = turnKeys[turnKeys.length - 1];
    // When an advanced filter is active, expand every matching turn --
    // the user is hunting for something, not skimming.
    const expandAll = ctxAdvActive();
    return turnKeys.map((t) => {
      const items = groups.get(t);
      const toks = items.reduce((s, x) => s + (x.b.token_estimate || 0), 0);
      // Collapse everything except the newest turn by default (or if the
      // user has toggled it).
      const collapsed = !expandAll && (ctxCollapsedTurns.has(t) || (t !== lastTurn && !ctxCollapsedTurns.has("open:" + t)));
      const heading = (t < 0) ? "Pre-conversation" : ("Turn " + t);
      return '<div class="turn-group' + (collapsed ? " collapsed" : "") + '" data-turn="' + t + '">'
        + '<div class="turn-head" onclick="toggleCtxTurn(' + t + ')">'
        + '<span class="turn-chev">&#9662;</span>'
        + '<span>' + heading + '</span>'
        + '<span class="turn-meta">' + items.length + ' block(s) &middot; ' + fmtTokens(toks) + ' tok</span>'
        + '</div>'
        + '<div class="turn-body">' + renderBlockSequence(items) + '</div>'
        + '</div>';
    }).join("");
  }

  // The collapsible advanced-filter bar: search, min-token threshold,
  // turn range, errors-only, hide-redacted. The legend swatches stay as
  // the quick category ("type of context") filter; this is everything
  // else. Inputs are plain onchange/oninput handlers (CSP-safe).
  function renderCtxAdvBar() {
    const turns = ctxTimeline.map((b) => (b.turn_n == null ? -1 : b.turn_n));
    const maxTurn = turns.length ? Math.max.apply(null, turns) : 0;
    const maxTok = ctxTimeline.length ? Math.max.apply(null, ctxTimeline.map((b) => b.token_estimate || 0)) : 0;
    const a = ctxAdv;
    const badge = ctxAdvActive() ? ' <span class="adv-badge">on</span>' : '';
    if (!a.open) {
      return '<button class="ctx-adv-toggle" onclick="ctxToggleAdvBar()">&#9881; Filters' + badge + '</button>';
    }
    return ''
      + '<div class="ctx-adv">'
      + '  <button class="ctx-adv-toggle open" onclick="ctxToggleAdvBar()">&#9881; Filters' + badge + '</button>'
      + '  <div class="ctx-adv-grid">'
      + '    <label class="ctx-adv-field ctx-adv-search">'
      + '      <span>Search text</span>'
      + '      <input type="search" id="ctx-adv-search" placeholder="content, label or category&hellip;" '
      +          'value="' + escapeAttr(ctxSearch) + '" oninput="ctxSetSearch(this.value)">'
      + '    </label>'
      + '    <label class="ctx-adv-field">'
      + '      <span>Min tokens: <b id="ctx-adv-mintok-val">' + a.minTokens + '</b></span>'
      + '      <input type="range" id="ctx-adv-mintok" min="0" max="' + Math.max(maxTok, 1) + '" step="1" '
      +          'value="' + a.minTokens + '" oninput="ctxSetMinTokens(this.value)">'
      + '    </label>'
      + '    <label class="ctx-adv-field">'
      + '      <span>Turns</span>'
      + '      <span class="ctx-adv-range">'
      + '        <input type="number" id="ctx-adv-turnfrom" min="-1" max="' + maxTurn + '" placeholder="from" '
      +            'value="' + (a.turnFrom == null ? "" : a.turnFrom) + '" onchange="ctxSetTurnRange(this.value, null)">'
      + '        <span>&ndash;</span>'
      + '        <input type="number" id="ctx-adv-turnto" min="-1" max="' + maxTurn + '" placeholder="to" '
      +            'value="' + (a.turnTo == null ? "" : a.turnTo) + '" onchange="ctxSetTurnRange(null, this.value)">'
      + '      </span>'
      + '    </label>'
      + '    <div class="ctx-adv-field ctx-adv-checks">'
      + '      <label><input type="checkbox" ' + (a.errorsOnly ? "checked" : "") + ' onchange="ctxSetFlag(\'errorsOnly\', this.checked)"> Errors only</label>'
      + '      <label><input type="checkbox" ' + (a.hideRedacted ? "checked" : "") + ' onchange="ctxSetFlag(\'hideRedacted\', this.checked)"> Hide redacted</label>'
      + '    </div>'
      + '  </div>'
      + (ctxAdvActive() ? '  <button class="ctx-clear-adv" onclick="ctxClearAdvanced()">Clear advanced filters</button>' : '')
      + '</div>';
  }

  function rerenderCtxView(fromInput) {
    const bar = document.getElementById("ctx-bar-wrap");
    const legend = document.getElementById("ctx-legend-wrap");
    const summary = document.getElementById("ctx-summary-wrap");
    const list = document.getElementById("ctx-block-list");
    const adv = document.getElementById("ctx-adv-wrap");
    if (bar) bar.innerHTML = renderContextBar(ctxTimeline);
    if (legend) legend.innerHTML = renderCtxLegend(ctxTimeline);
    if (summary) summary.innerHTML = renderCtxFilterSummary(ctxTimeline);
    if (list) list.innerHTML = renderCtxBlocks(ctxTimeline);
    // Re-rendering the adv bar on every keystroke would steal focus from
    // the search box, so skip it when the change came from an input in
    // the bar itself -- only its little derived readouts need updating.
    if (adv && !fromInput) { adv.innerHTML = renderCtxAdvBar(); }
    else if (fromInput) {
      const mv = document.getElementById("ctx-adv-mintok-val");
      if (mv) mv.textContent = ctxAdv.minTokens;
    }
  }

  function toggleCtxCategory(key) {
    if (ctxHiddenCats.has(key)) ctxHiddenCats.delete(key);
    else ctxHiddenCats.add(key);
    saveHiddenCats(ctxHiddenCats);
    rerenderCtxView();
  }
  function ctxFilterAll() { ctxHiddenCats.clear(); saveHiddenCats(ctxHiddenCats); rerenderCtxView(); }
  function ctxFilterNone() {
    ctxHiddenCats = new Set(CTX_CATEGORIES.map(([k]) => k));
    saveHiddenCats(ctxHiddenCats);
    rerenderCtxView();
  }
  function ctxToggleAdvBar() { ctxAdv.open = !ctxAdv.open; saveAdvFilter(); rerenderCtxView(); }
  function ctxSetSearch(v) { ctxSearch = (v || "").trim().toLowerCase(); rerenderCtxView(true); }
  function ctxSetMinTokens(v) { ctxAdv.minTokens = Math.max(0, parseInt(v, 10) || 0); saveAdvFilter(); rerenderCtxView(true); }
  function ctxSetTurnRange(from, to) {
    if (from !== null) ctxAdv.turnFrom = (from === "" ? null : parseInt(from, 10));
    if (to !== null) ctxAdv.turnTo = (to === "" ? null : parseInt(to, 10));
    saveAdvFilter();
    rerenderCtxView(true);
  }
  function ctxSetFlag(name, on) { ctxAdv[name] = !!on; saveAdvFilter(); rerenderCtxView(true); }
  function ctxClearAdvanced() {
    ctxSearch = "";
    ctxAdv.minTokens = 0; ctxAdv.turnFrom = null; ctxAdv.turnTo = null;
    ctxAdv.errorsOnly = false; ctxAdv.hideRedacted = false;
    saveAdvFilter();
    rerenderCtxView();
  }
  function toggleCtxTurn(t) {
    // Track both directions explicitly so a turn the user opened stays
    // open across a re-render, and one they collapsed stays collapsed.
    if (ctxCollapsedTurns.has(t)) {
      ctxCollapsedTurns.delete(t);
      ctxCollapsedTurns.add("open:" + t);
    } else if (ctxCollapsedTurns.has("open:" + t)) {
      ctxCollapsedTurns.delete("open:" + t);
      ctxCollapsedTurns.add(t);
    } else {
      // was showing (newest turn) -> collapse it
      ctxCollapsedTurns.add(t);
    }
    rerenderCtxView();
  }

  function renderContextTab(timeline) {
    if (!timeline.length) {
      return '<p class="dash-empty">No context_blocks for this session: record_session was called without the optional field.</p>';
    }
    ctxTimeline = timeline;
    ctxSearch = "";  // search text never carries across a session switch
    ctxCollapsedTurns.clear();
    return `
      <div class="agent-tabs">
        <span class="agent-tab active">main<span class="a-tok">` + fmtTokens(timeline[timeline.length - 1].cumulative_tokens) + ` tok</span></span>
      </div>
      <div class="ctx-bar" id="ctx-bar-wrap">` + renderContextBar(timeline) + `</div>
      <div id="ctx-legend-wrap">` + renderCtxLegend(timeline) + `</div>
      <div class="section-heading">Context blocks</div>
      <div id="ctx-adv-wrap">` + renderCtxAdvBar() + `</div>
      <div id="ctx-summary-wrap">` + renderCtxFilterSummary(timeline) + `</div>
      <div class="block-list" id="ctx-block-list">` + renderCtxBlocks(timeline) + `</div>
    `;
  }

  function renderOverviewTab(detail) {
    const m = detail.metrics.prompt_metrics;
    // "Lines changed" / "Active time" from the mockup have no backing
    // schema field, omitted rather than shown as fake zeros.
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
  }

  function cacheHitPct(turns) {
    // Anthropic's usage accounting: cache_read_input_tokens is a
    // SEPARATE bucket from input_tokens (the fresh/uncached portion),
    // not a subset of it. A turn that's almost entirely served from
    // cache can have cache_read_input_tokens far exceed input_tokens
    // (e.g. read=22134, input=2). Dividing read/input (found via a live
    // browser E2E test, where a real session rendered "4184%") can exceed
    // 100%; the correct hit rate is read's share of the turn's TOTAL
    // input (read + fresh), which is always <= 100%.
    if (!turns || !turns.length) return "n/a";
    let read = 0, fresh = 0;
    turns.forEach((t) => { read += t.cache_read_input_tokens || 0; fresh += t.input_tokens || 0; });
    const total = read + fresh;
    if (!total) return "n/a";
    return Math.round((read / total) * 100) + "%";
  }

  function statusBadge(status) {
    const cls = status === "success" || status === "ok" ? "ok" : (status === "error" ? "err" : "dim");
    return '<span class="badge-pill ' + cls + '"><span class="status-dot ' + cls + '"></span>' + status + '</span>';
  }

  function renderToolsTab(trace) {
    if (!trace.length) {
      return '<p class="dash-empty">No tool calls recorded for this session.</p>';
    }
    const rows = trace.map((c, i) => `
      <div class="tool-row">
        <span class="mono">` + (i + 1) + `</span>
        <span>` + c.tool + `</span>
        <span>` + statusBadge(c.status) + `</span>
        <span class="mono">` + (c.latency_ms || 0) + `ms</span>
        <span class="args mono">` + JSON.stringify(c.args || {}).slice(0, 80) + `</span>
        <span class="mono">` + timeAgo(c.timestamp) + `</span>
      </div>`).join("");
    return `
      <div class="tool-table">
        <div class="tool-row tool-head"><span>#</span><span>Tool</span><span>Status</span><span>Latency</span><span>Args</span><span>Time</span></div>
        ` + rows + `
      </div>`;
  }

  function renderReliabilitySubpanel(trace) {
    // Real data, computed client-side from this session's already-
    // fetched trace (grouped by tool, ok vs. error counts). Nothing new
    // added server-side for this.
    if (!trace.length) {
      return '<div class="subpanel"><h4>Tool reliability, this session</h4><p class="dash-empty">No tool calls recorded.</p></div>';
    }
    const byTool = {};
    trace.forEach((c) => {
      const t = byTool[c.tool] || (byTool[c.tool] = {ok: 0, err: 0});
      if (c.status === "success" || c.status === "ok") t.ok += 1; else t.err += 1;
    });
    const rows = Object.entries(byTool).map(([tool, counts]) => {
      const total = counts.ok + counts.err;
      const errPct = total ? (counts.err / total * 100) : 0;
      return `
        <div class="bar-row">
          <span class="b-name">` + tool + `</span>
          <div class="bar-track"><div class="bar-fill` + (errPct > 0 ? " err" : "") + `" style="width:` + (100 - errPct) + `%;"></div></div>
          <span class="b-val">` + counts.ok + `/` + total + `</span>
        </div>`;
    }).join("");
    return '<div class="subpanel"><h4>Tool reliability, this session</h4>' + rows + '</div>';
  }

  function renderBreakdownTab(trace) {
    const notTracked = (title) => '<div class="subpanel"><h4>' + title + '</h4><p class="dash-empty">Not tracked yet.</p></div>';
    return `
      <div class="two-col">
        ` + renderReliabilitySubpanel(trace) + `
        ` + notTracked("Spend by subagent / skill") + `
        ` + notTracked("MCP server connections") + `
        ` + notTracked("Reliability signals / API errors") + `
      </div>
    `;
  }

  function renderSessionDetail(sessionId, detail, timeline) {
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
  }

  function showDetailTab(name, btn) {
    const panel = btn.closest(".panel");
    if (!panel) return;
    panel.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
    panel.querySelectorAll(".tab-content").forEach((c) => c.classList.toggle("active", c.dataset.content === name));
  }

  async function selectSession(evt) {
    const sessionId = evt.currentTarget.dataset.id;
    dashboardSelected = sessionId;
    document.querySelectorAll(".session-row").forEach((r) => r.classList.toggle("active", r.dataset.id === sessionId));
    const detailEl = document.getElementById("detail-panel");
    if (!detailEl) return;
    detailEl.innerHTML = '<p class="dash-empty">Loading…</p>';
    try {
      const token = document.getElementById("dash-root").dataset.token;
      const [detail, timeline] = await Promise.all([
        apiGet(token, "/api/sessions/" + sessionId),
        apiGet(token, "/api/context-timeline/" + sessionId),
      ]);
      detailEl.innerHTML = renderSessionDetail(sessionId, detail, timeline);
    } catch (err) {
      detailEl.innerHTML = '<p class="dash-error">Failed to load session: ' + escapeHtml(err.message) + '</p>';
    }
  }

  function renderDashboardShell() {
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
  }

  function refreshDashboardPanels() {
    document.getElementById("kpi-strip").innerHTML = renderKpiStrip();
    document.getElementById("insight-strip").innerHTML = renderInsightStrip();
    document.getElementById("session-list-panel").innerHTML = renderSessionListPanel();
  }

  function setDashboardRange(range) {
    dashboardRange = range;
    document.getElementById("range-row").innerHTML = renderRangeRow();
    refreshDashboardPanels();
  }

  function setSourceFilter(source) {
    dashboardSourceFilter = source;
    refreshDashboardPanels();
  }

  async function refreshDashboard(token, { force = false } = {}) {
    const root = document.getElementById("dash-root");
    if (!root) { clearInterval(dashboardTimer); return; }
    // Skip an automatic poll while the tab is backgrounded. A manual
    // refresh (force) or the visibilitychange resume still goes through.
    if (!force && document.hidden) return;
    const btn = document.getElementById("manual-refresh-btn");
    if (btn) { btn.disabled = true; btn.classList.add("spinning"); }
    try {
      const testParam = dashboardIsDevMode && dashboardShowTestSessions ? "&include_test_sessions=1" : "";
      dashboardSessions = await apiGet(token, "/api/sessions?limit=" + DASHBOARD_SESSION_LIMIT + testParam);
      if (!document.getElementById("kpi-strip")) {
        renderDashboardShell();
      } else {
        refreshDashboardPanels();
      }
      const listEl = document.getElementById("session-list");
      if (listEl && !sessionsInRange().some((s) => s.session_id === dashboardSelected)) {
        const row = listEl.querySelector(".session-row");
        if (row) row.click();
      }
    } catch (err) {
      root.innerHTML = '<p class="dash-error">Failed to load sessions: ' + escapeHtml(err.message) + '</p>';
    } finally {
      // Panels (including this button) get fully re-rendered above on
      // success, so this only matters on the error path. Re-query
      // rather than reuse `btn`, which may already be a detached node.
      const freshBtn = document.getElementById("manual-refresh-btn");
      if (freshBtn) { freshBtn.disabled = false; freshBtn.classList.remove("spinning"); }
    }
  }

  function manualRefresh() {
    const root = document.getElementById("dash-root");
    const token = root && root.dataset.token;
    if (token) refreshDashboard(token, { force: true });
  }

  function toggleAutoRefresh() {
    dashboardAutoRefresh = !dashboardAutoRefresh;
    if (dashboardTimer) {
      clearInterval(dashboardTimer);
      dashboardTimer = null;
    }
    if (dashboardAutoRefresh) {
      const root = document.getElementById("dash-root");
      const token = root && root.dataset.token;
      if (token) dashboardTimer = setInterval(() => refreshDashboard(token), DASHBOARD_POLL_MS);
    }
    // Panels re-render on the next refresh already, but toggling should
    // reflect the new state immediately even if a full poll is a while away.
    const panel = document.getElementById("session-list-panel");
    if (panel) panel.innerHTML = renderSessionListPanel();
  }

  function toggleTestSessions() {
    dashboardShowTestSessions = !dashboardShowTestSessions;
    const root = document.getElementById("dash-root");
    const token = root && root.dataset.token;
    if (token) refreshDashboard(token, { force: true });
  }

  // One poll loop per page load; re-mounting (e.g. signing in again)
  // clears the previous timer instead of stacking a second one.
  function mountDashboard(token) {
    const root = document.getElementById("dash-root");
    if (root) root.dataset.token = token;
    dashboardSelected = null;
    dashboardSessions = [];
    dashboardAutoRefresh = true;
    dashboardIsDevMode = false;
    dashboardShowTestSessions = false;
    if (dashboardTimer) clearInterval(dashboardTimer);
    renderDashboardShell();
    apiGet(token, "/api/dev-mode-status")
      .then((data) => { dashboardIsDevMode = !!data.dev_mode; })
      .catch(() => {})
      .finally(() => refreshDashboard(token, { force: true }));
    dashboardTimer = setInterval(() => refreshDashboard(token), DASHBOARD_POLL_MS);
    // When the tab comes back to the foreground, pull once immediately so
    // the list isn't stale up to a full poll interval, then let the timer
    // (which was firing into a no-op while hidden) resume normally.
    if (!dashboardVisibilityHooked) {
      document.addEventListener("visibilitychange", () => {
        if (!document.hidden) {
          const r = document.getElementById("dash-root");
          const t = r && r.dataset.token;
          if (t) refreshDashboard(t, { force: true });
        }
      });
      dashboardVisibilityHooked = true;
    }
  }

  // --- Project settings (new UI, no backend yet) ----------------------
  // TODO: wire to a real per-project settings endpoint once one exists.
  // Every control below is inert. This screen exists so the settings
  // UX is visually complete and navigable via the ⚙ toggle, matching
  // the mockup, but nothing here persists across a page reload.
  function renderSettingsScreen() {
    return `
      <div class="settings-wrap">
        <div class="settings-head">
          <h3>Project settings</h3>
          <p>Alert thresholds, redaction/retention, and session labels. Nothing here is wired to a backend yet.
          changes made here are not saved.</p>
        </div>
        <div class="disclosure-note">
          <span>&#9888;</span>
          <span><strong>Not yet persisted.</strong> This screen is UI-complete but every control below is
          disconnected from a real settings store, so reloading the page resets it.</span>
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
  }

  function toggleSettings() {
    const dashRoot = document.getElementById("dash-root");
    const settingsRoot = document.getElementById("settings-root");
    if (!dashRoot || !settingsRoot) return;
    const showingSettings = settingsRoot.classList.contains("hidden");
    if (showingSettings) {
      settingsRoot.innerHTML = renderSettingsScreen();
      dashRoot.classList.add("hidden");
      settingsRoot.classList.remove("hidden");
    } else {
      settingsRoot.classList.add("hidden");
      settingsRoot.innerHTML = "";
      dashRoot.classList.remove("hidden");
    }
  }

  // Decodes a Google ID token's payload for DISPLAY only (email, in the
  // consent screen). This is NOT verification. The signature is checked
  // server-side in /auth/verify, which is the only place this credential
  // is trusted for anything security-relevant.
  //
  // Duplicated verbatim in sre-investigation-agent's web/chat.js (same
  // function, same purpose, its own consent flow), deliberately not
  // shared, since these are two different repos/origins with no build
  // step between them. Fix bugs in both copies.
  function decodeJwtPayloadForDisplay(token) {
    try {
      const base64 = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
      const json = decodeURIComponent(
        atob(base64).split("").map((c) => "%" + c.charCodeAt(0).toString(16).padStart(2, "0")).join("")
      );
      return JSON.parse(json);
    } catch {
      return {};
    }
  }

  let pendingCredential = null;
  let currentEmail = null;
  let currentToken = null;

  function consentPage(email) {
    const initial = avatarInitial(email);
    return `
      <div class="handshake">
        <div class="icon-circle">◈</div>
        <span class="arrow">┅┅┅&gt;</span>
        <div class="icon-circle accent">◈<span class="badge-check">✓</span></div>
      </div>
      <h1 class="consent-title">Connect to CtxWindow</h1>
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
          <div class="permission-row"><span class="dot"></span> Nothing else: no access to anyone else's data, ever</div>
        </div>
      </div>
      <div class="btn-row" style="margin-top: 1.25rem;">
        <button class="btn-secondary" onclick="cancelConsent()">Cancel</button>
        <button class="btn-primary" onclick="authorize()">Authorize</button>
      </div>
    `;
  }

  function successBanner(email) {
    return `
      <div class="success-banner">
        <div class="icon-circle accent">◈<span class="badge-check">✓</span></div>
        <div>
          <h2>Authorization granted</h2>
          <p>Signed in as ` + email + `. Everything below is scoped to your account only.</p>
        </div>
      </div>
      <p class="card-hint" style="text-align:center; margin: -0.6rem 0 1.4rem;">
        Your token, your sessions, your local Claude Code config: all of it stays on this
        computer. Nothing you set up below is ever sent anywhere except this server.
      </p>
    `;
  }

  function onSignIn(response) {
    pendingCredential = response.credential;
    const { email } = decodeJwtPayloadForDisplay(response.credential);
    document.getElementById("intro").classList.add("hidden");
    const landing = document.getElementById("landing");
    landing.classList.remove("hidden");
    landing.innerHTML = consentPage(email || "your Google account");
  }

  function cancelConsent() {
    pendingCredential = null;
    document.getElementById("landing").classList.add("hidden");
    document.getElementById("intro").classList.remove("hidden");
  }

  async function authorize() {
    if (!pendingCredential) return;
    const landing = document.getElementById("landing");
    landing.innerHTML = "<p class=\"sub\">Authorizing…</p>";
    try {
      const res = await fetch("/auth/verify", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({credential: pendingCredential}),
      });
      const data = await res.json();
      pendingCredential = null;
      if (res.ok) {
        // --- webapp/ auth handoff -----------------------------------
        // Minimal, isolated addition: the mobile webapp (webapp/app.js,
        // served at /m) reuses this same Google sign-in flow instead of
        // building a second one. It sends users here with
        // ?return_to=/m; if present, skip the desktop consent/dashboard
        // screens entirely and bounce straight back with the freshly
        // minted token in the URL fragment (never the query string, so
        // it doesn't land in server logs). webapp/app.js reads it once,
        // stores it in its own localStorage key, and strips it from the
        // URL bar. See webapp/app.js's consumeTokenFromLocation().
        // startsWith("/") alone would also admit "//evil.com", which browsers
        // treat a leading "//" as a protocol-relative absolute URL, which
        // would carry the token off-origin in the fragment. Requiring a
        // single "/" not followed by another "/" restricts this to a real
        // same-origin relative path.
        const returnTo = new URLSearchParams(window.location.search).get("return_to");
        if (returnTo && /^\/(?!\/)/.test(returnTo)) {
          window.location.href = returnTo + "#token=" + encodeURIComponent(data.mcp_token) + "&email=" + encodeURIComponent(data.email);
          return;
        }
        persistSession(data.mcp_token, data.email);
        currentEmail = data.email;
        currentToken = data.mcp_token;
        landing.innerHTML = successBanner(data.email) + connectPage(data.email, data.mcp_token);
        refreshInstallCommand();
      } else {
        landing.innerHTML = "<div class='card security'>Sign-in failed: " + escapeHtml(data.error || "unknown error") + "</div>";
      }
    } catch (err) {
      pendingCredential = null;
      landing.innerHTML = "<div class='card security'>Sign-in failed: " + escapeHtml(err.message) + "</div>";
    }
  }

  // --- Browser persistence ---------------------------------------------
  // localStorage (not sessionStorage), which deliberately survives closing
  // the browser entirely, same trust model as staying signed into any
  // other Google-backed site: whoever authorized here once sees their
  // own dashboard again next visit with no re-auth, until they sign out
  // or the token is revoked server-side (see the README's "Can this
  // token be revoked?"). Nothing else is ever stored here; the token
  // itself is the only credential, same one shown in the "Your
  // connection" card and handed to your MCP client's config.
  const SS_TOKEN = "mci_token";
  const SS_EMAIL = "mci_email";

  function persistSession(token, email) {
    localStorage.setItem(SS_TOKEN, token);
    localStorage.setItem(SS_EMAIL, email);
  }

  function signOut() {
    localStorage.removeItem(SS_TOKEN);
    localStorage.removeItem(SS_EMAIL);
    // Best-effort: also drop the httpOnly session cookie so a reload
    // doesn't silently sign back in via /auth/session.
    fetch("/auth/logout", { method: "POST" }).catch(() => {});
    pendingCredential = null;
    currentEmail = null;
    currentToken = null;
    dashboardSelected = null;
    backToConnect();
    document.getElementById("dashboard-screen").innerHTML = "";
    document.getElementById("landing").classList.add("hidden");
    document.getElementById("landing").innerHTML = "";
    document.getElementById("intro").classList.remove("hidden");
  }

  // Distinguishes "this token is dead" (401/403 -> sign out, it was
  // revoked server-side) from "the server hiccuped" (5xx, 429, a network
  // blip, offline, an extension blocking the request -> keep the stored
  // token, the user is still signed in, just retry on the next load or
  // dashboard refresh). Previously ANY non-2xx or network failure on the
  // load-time probe wiped localStorage and forced a full Google
  // re-login: a single Cloud Run cold-start 503 or a moment offline was
  // enough, which is the "logged out several times a week" report.
  async function verifyStoredToken(token) {
    // Bound the probe: a hung connection (captive portal, an extension
    // that stalls rather than fails the request) must not leave the
    // install card spinning forever -- after ~6s we treat it like any
    // other transient failure and let the dashboard's own refresh cycle
    // retry. AbortController is used where available; older engines just
    // wait on the fetch.
    let res;
    const ctl = typeof AbortController !== "undefined" ? new AbortController() : null;
    const timer = ctl ? setTimeout(() => ctl.abort(), 6000) : null;
    try {
      res = await fetch("/api/sessions?limit=1", {
        headers: { Authorization: "Bearer " + token },
        signal: ctl ? ctl.signal : undefined,
      });
    } catch (e) {
      return "transient";  // offline / DNS / connection reset / aborted (timeout)
    } finally {
      if (timer) clearTimeout(timer);
    }
    if (res.status === 401 || res.status === 403) return "revoked";
    return "ok";  // 2xx, or a 5xx/429 we choose to ride out
  }

  // Runs once on load. A stored token is trusted enough to go straight
  // to the dashboard screen (no flash of the sign-in screen for a
  // returning visitor), but then verified with a real request. A
  // token revoked server-side since the last visit signs this browser
  // back out instead of showing a dashboard that just 401s on every
  // fetch. The connect/config cards render into #landing too (kept
  // hidden) so "Token & config" on the dashboard topbar has content
  // ready without a page reload.
  //
  // Fallback path: if localStorage was cleared (private window, "clear
  // site data", a different browser profile) but the httpOnly session
  // cookie set at /auth/verify is still valid, /auth/session hands the
  // token straight back with no Google re-prompt.
  async function rehydrateFromStorage() {
    let token = localStorage.getItem(SS_TOKEN);
    let email = localStorage.getItem(SS_EMAIL);
    if (!token || !email) {
      try {
        const r = await fetch("/auth/session");  // sends the mci_session cookie
        if (r.ok) {
          const d = await r.json();
          if (d && d.mcp_token && d.email) {
            token = d.mcp_token;
            email = d.email;
            persistSession(token, email);
          }
        }
      } catch (e) { /* fall through to the sign-in screen */ }
    }
    if (!token || !email) return;
    currentEmail = email;
    currentToken = token;
    document.getElementById("intro").classList.add("hidden");
    const landing = document.getElementById("landing");
    landing.classList.remove("hidden");
    landing.innerHTML = successBanner(email) + connectPage(email, token);
    goToDashboard();
    // Verify BEFORE minting an install command: a revoked stored token
    // would make /setup/issue-install-code 401 and flash that error in
    // the setup card just before this check signs the browser out.
    verifyStoredToken(token).then((state) => {
      if (state === "revoked") {
        signOut();
        return;  // don't mint against a token we just found dead
      }
      // "transient" / "ok" -> stay signed in; dashboard fetches retry on
      // their own refresh cycle.
      refreshInstallCommand();
    });
  }

  rehydrateFromStorage();
