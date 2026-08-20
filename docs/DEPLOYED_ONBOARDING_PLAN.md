# Plan: automatic setup for deployed-instance-first users

**Status: planning only — nothing in this doc is built yet.** Written for a
fresh context window; self-contained, but cross-references
`docs/USER_JOURNEY.md` (the audit that found the gaps this plan addresses)
and `mcp_server/server.py`'s `apply_local_config` route (the local-only
feature this plan generalizes).

## The problem, precisely

Two things exist today:

1. **One-click local setup** (`POST /setup/apply-local-config`) — writes
   MCP config + OTLP telemetry env vars into `~/.claude/settings.json`
   automatically. **Only works when the server itself is running on the
   user's own machine** (gated to loopback requests), because the write
   happens *inside the server process*, which needs real filesystem access
   to the caller's home directory. A deployed instance (Cloud Run) has no
   such access — it's a different machine entirely.
2. **claude.ai Connectors** — genuinely automatic, works for the deployed
   instance, zero local files touched. But it can only register an MCP
   server connection. It has no mechanism to set local shell/session
   environment variables, so it **cannot** deliver OTLP auto-telemetry
   (`CLAUDE_CODE_ENABLE_TELEMETRY` and friends) — that capability is
   structurally outside what a Connector *is*.

So today, a deployed-instance-first user (the intended default going
forward) has a real path to (1) but not (2) automatically. This plan is
about closing (2) without requiring them to run the server locally.

## The core idea: a personalized, downloadable local setup script

The insight: **"one-click automatic setup" and "the server runs on my
machine" are two separate things that happen to be coupled today only
because of how the feature was first built.** They don't have to be.

`apply_local_config`'s actual logic — read `~/.claude/settings.json`, back
it up, merge in an `mcpServers` entry and an `env` block, write it back — is
just JSON manipulation on a local file. It doesn't need to run *inside* the
deployed server process. It only needs to run *once, on the user's own
machine*, which is exactly what a script the user downloads and runs
locally already does — no self-hosting required, because the actual MCP
server and dashboard stay right where they are (deployed).

**Proposed flow:**

1. User signs in on the deployed instance (unchanged — same Google flow,
   same bearer token they already get today).
2. Connect page offers **"Download setup script"** as the primary,
   recommended action (see the redesign in §3).
3. Clicking it does an authenticated `fetch()` (same bearer token already
   in the page, same pattern as the existing "Copy config" buttons) to a
   new endpoint, e.g. `GET /setup/local-script`, which returns a **plain
   text script**, not JSON — the deployed server generates this file
   per-request, with the user's own token and the deployed URLs baked in.
4. The browser triggers a file download (`Blob` + a synthetic `<a
   download>` click — standard pattern, no new browser API needed).
5. User inspects (encouraged, see §4) and runs the script once, locally.
   It performs the exact same backup-then-merge write `apply_local_config`
   already does today — just executed by a standalone script instead of by
   the (in this case, remote) server process.

**What this buys:** the deployed instance becomes capable of delivering
*both* halves of "auto-connected everywhere" — MCP query access *and* OTLP
telemetry — without ever asking the user to `git clone` and run anything
long-lived on their own machine. The only local execution is a one-time,
inspectable script.

## Handshake design — how the script gets the user's token safely

This needs care, because a downloadable file containing a bearer token is a
different risk shape than a token displayed in an authenticated page.

- **The script must be generated per-request, from an authenticated call —
  never a static, shareable URL.** If `/setup/local-script` were a plain
  `GET` link, anyone who obtained that URL (browser history, a shared
  screenshot, a proxy log) could download a script containing someone
  else's live bearer token. Route it through the same `Authorization:
  Bearer <token>` header pattern every other protected route already uses
  (`MultiTokenAuthMiddleware`), fetched via JS, not linked directly.
- **The script should say plainly, in a comment at the top, what it does
  and that it contains a live credential.** E.g.: `# This script contains
  your personal mcp-context-inspector token. Treat it like a password —
  don't share this file, and delete it after running.`
- **Consider a short-lived variant for extra safety**: instead of embedding
  the user's long-lived token directly, the endpoint could mint a
  single-use setup token (valid for e.g. 10 minutes, exchanged for the real
  token only when the script actually runs and calls back to the server
  once). This adds real complexity (a new token type, a new exchange
  endpoint, expiry handling) for a marginal security gain over "it's a
  file on your own disk, same trust level as your SSH key" — **recommend
  starting with the simple embedded-token version, revisit only if this
  project's threat model changes** (e.g. if multi-tenant/shared-machine use
  becomes common).
- **After running, the script should tell the user to delete it** (it has
  no further use — the credential now also lives in `settings.json`, and a
  second copy sitting in `~/Downloads` is unnecessary exposure).
- **Do not pipe-to-shell** (`curl ... | sh`). That pattern is a well-known
  trust smell even when the source is honest, because it denies the user
  any chance to read what they're running before it runs. Require an
  explicit "Download" then "run it yourself" — matches this project's own
  stated values (transparency, "your data, your machine") better than
  convenience would.

## Script implementation notes

- **Language**: Python, not bash. This project already assumes Python
  (it's a Python server); the merge logic already exists in
  `apply_local_config` as real Python and can be extracted into a shared
  helper (e.g. `mcp_server/local_setup.py`) called by *both* the
  server-side route (self-host case) and templated into the downloadable
  script (deployed case) — one implementation, not two to keep in sync.
  Bash+`jq` would work too but forces a `jq` dependency check; Python 3 is
  already required to run this project at all, and is present on every
  platform Claude Code itself supports.
- **Windows**: `~/.claude/settings.json` resolves differently
  (`%USERPROFILE%\.claude\settings.json`). `pathlib.Path.home()` already
  handles this correctly in Python — no special-casing needed if the
  script is Python, one more argument for that choice over a bash script.
- **Idempotency**: running the script twice should be a safe no-op (or a
  harmless re-write of the same values) — matches the existing
  `apply_local_config` merge behavior already.

## claude.ai Connectors — formalize as a documented, supported path

Already shipped this session (the connect page's non-local branch now
gives real steps), and already covered by `docs/USER_JOURNEY.md` §4. This
plan's only addition: once the download-script flow above exists, the
connect page should present **both** paths side by side with a clear
one-line distinction, not stacked as primary/fallback:

- **claude.ai Connectors** → MCP query tools, every client, zero local
  files. *(Doesn't cover telemetry.)*
- **Download setup script** → MCP query tools *and* auto-telemetry, one
  local script run. *(Covers everything, needs one local action.)*

Most users will want the script for the complete experience; Connectors is
the right answer for someone who only wants query access, or who's on a
machine where running a downloaded script isn't appropriate (e.g. a
locked-down work laptop).

## §3 — Redesigning "Land on config" to be crystal clear

Current state (as of this session): three stacked cards — "Your
connection", "Set up Claude Code automatically" (self-host-only button, or
now the Connectors steps), and a 4-tab manual card — with no visual
hierarchy signaling which one a new user should actually use.

**Proposed structure**, in priority order top to bottom:

1. **"Your connection"** stays as-is — token + URL, always relevant,
   always shown.
2. **One clearly-labeled primary action card**, adapting to context:
   - Self-hosted (localhost): today's "Apply to my Claude Code config"
     button, unchanged.
   - Deployed: **"Download setup script"** as the big, obviously-primary
     button — not a paragraph of prose with a small button at the end.
     Directly below it, in smaller/dimmer text: *"Prefer not to run a
     script? Connect via claude.ai Connectors instead →"* as a secondary
     link, not a second full card competing for attention.
3. **Everything manual collapsed by default** — the 4-tab card (MCP
   config / API-curl / Claude Code telemetry / Copilot telemetry) becomes
   a single `<details>` disclosure: *"Advanced: manual setup, or connecting
   a different client"* — closed by default, so a first-time visitor sees
   one clear recommended action, not four tabs to choose between with no
   guidance on which.
4. **The "Live dashboard" card** stays below, as today.

This directly targets the "crystal clear for the human eye" requirement:
one obvious button, one line of alternative, everything else out of the
way until asked for.

## Open questions to resolve before building

1. Should the downloaded script have a `.py` extension (requires the user
   to know to run `python3 script.py`) or should the connect page also
   show the one-line run command right next to the download button, so
   there's zero ambiguity about how to execute it?
2. Does this project want to support the short-lived-token handshake
   variant now, or explicitly defer it (recommended: defer, per the
   reasoning in the handshake section above) — worth a one-line decision
   recorded here once made, so it isn't re-litigated later.
3. Should `apply_local_config`'s logic actually be extracted into a shared
   module now (so the future script-generation endpoint has something to
   import), or is that refactor better done at the same time the new
   endpoint is actually built? (Recommend: same time — extracting it now
   with no second caller yet is premature.)

## Suggested build order, when this moves from plan to implementation

1. Extract the merge/backup logic from `apply_local_config` into a shared
   helper function.
2. Build `GET /setup/local-script`, authenticated, returning the
   templated Python script as `text/plain` with a `Content-Disposition:
   attachment` header so the browser downloads rather than navigates.
3. Frontend: download button + the one-line run command shown next to it.
4. Redesign the connect page per §3 (primary action + collapsed advanced
   section) — do this alongside step 3, since the new button needs a home.
5. Update `docs/USER_JOURNEY.md` and `docs/AUTH.md` once shipped, the same
   way this session documented the local-only version.
