# Lumen launch plan — from working pipe to real product

Telemetry works end to end now — confirmed, one real session, live on the dashboard. What's
left isn't more debugging, it's turning a five-step manual config edit into something a
stranger can do in under a minute. This is untracked on purpose — a working doc, not part of
the repo's history.

> **Reviewed by an independent fresh-eyes pass against the actual code.** Caught one real bug
> worth flagging up front: `GET /otlp/debug`, the endpoint "Test your connection" was going to
> rely on, is **not** scoped to `current_owner` today — it's global process state. That's now a
> required fix (§2, §4), not a documentation item. Six smaller corrections are folded in below,
> each marked **"Correction from review"**.

## 0. Where this stands today

**Current journey:**
1. Sign in with Google at `/auth/login`
2. Click "Download setup script"
3. Open a terminal, `cd` to Downloads
4. Run `python3 mcp-context-inspector-setup.py`
5. Manually delete the script (it holds a bearer token)
6. Restart Claude Code for the new env vars to load

**What we're moving to:**
1. Sign in with Google
2. Copy 2–3 lines the page hands you
3. Paste into a terminal, done — no download, no manual delete step
4. Close any existing Claude Code sessions, open a fresh one
5. Run a prompt, then check "Test my connection" on the page

## 1. The new journey, step by step

**1. Sign in with Google — unchanged.** Already works. This is the identity anchor everything
else hangs off: a `google_sub` minting one bearer token per person, stored in `mcp_users`.
Nothing to build here.

**2. The page hands over a one-line install command, not a file.** Same shape as the Claude
Code install itself. Directly above the command, in bold italics: ***Please close any existing
Claude Code sessions*** — terminal windows or editor integrations — before running this, since
env vars only load once at process startup and an already-open session won't pick up the new
config no matter how correct the file on disk now is:

```
curl -fsSL https://mcp-inspector.sohaibsohail.workers.dev/setup/install?t=<short-lived-code> | sh
```

The bearer token is exchanged via a short-lived code, not pasted into the command in
plaintext — a plaintext token in a piped command ends up in shell history forever.

Directly beneath, an expandable "Not comfortable piping straight into a shell? Inspect it
first" block — collapsed by default, same page, no separate URL — reveals the
download-then-run alternative:

```
curl -fsSL …/setup/install?t=<code> -o install.sh
less install.sh        # read exactly what it's about to do
sh install.sh
```

It's the identical file either way — this isn't a second code path to maintain, just a second
way of inviting someone to run the same one. `-o install.sh` instead of piping lets a
terminal, editor, or `less` show the real contents before anything executes; the piped
one-liner stays the default and visible option because it's the fastest path for anyone who
already trusts the source — the same tradeoff Homebrew and rustup make.

**3. The script detects the platform and applies the patch itself.** One script, not three —
branches internally on `uname` for macOS/Linux, with a PowerShell sibling served instead based
on the page's platform toggle (not UA sniffing — see build list). It does exactly what
`local_setup.py` already does (backup, merge, never overwrite) — this is a delivery-mechanism
change, not a logic change, *provided* the shell script shells out to the existing Python merge
logic rather than reimplementing JSON backup/merge in shell. Two things to pin down before
writing it: the piped form (`curl … | sh`) invokes `sh` explicitly, so the shebang line is
ignored — the script body itself must be POSIX `sh`, not assume `bash`; and if it shells out to
`python3`, that's a dependency worth stating rather than assuming. No leftover file with a
token in it: it fetches, applies, and exits. It also prints its own reminder to close and
reopen Claude Code, right in the same terminal, in case the notice above the command was
missed — belt and suspenders, since this was the single most likely "I ran it and nothing
happened" support question during the original fix — and confirms it's safe to re-run
(idempotent merge, matching what the current Python script already tells people).

**4. "Test your connection" — stationary until it's real, with a third state for "sent but
rejected".** No polling loop, no spinner pretending to work — a single static line:
*"Waiting for your first prompt…"* Claude Code only exports telemetry on actual use, so
there's no honest way to fake a heartbeat here. The panel sits still until the user runs one
fresh prompt, then checks on refresh (about 10 seconds after the prompt finishes, matching the
script's own `OTEL_LOGS_EXPORT_INTERVAL=5000` — a concrete number beats an open-ended wait).

> **Correction from review:** `GET /otlp/debug` is *not* scoped today — it returns
> process-global counters (`otlp/__init__.py`'s `_counts`/`_last_accepted_at`/`_recent_skipped`
> are never keyed by owner, and the route never reads `current_owner`). Two consequences if
> shipped as-is: (a) the panel would flip to "connected" for User B the instant User A sends
> any session — a false positive; (b) `recent_skipped` currently exposes other tenants'
> `resource_attrs` (hostnames, session IDs) to any signed-in caller. This has to become
> genuinely owner-scoped before this panel ships — see §2, now a required build item.

Once scoped, add a third state beyond waiting/confirmed: if a payload is arriving but landing
in `recent_skipped` (vendor undetected — the same failure this project already hit once,
root-caused as a missing `OTEL_RESOURCE_ATTRIBUTES`), say so explicitly: *"We're receiving data
from your machine but can't identify it as Claude Code — re-run the install command."* The data
for this already exists; it's a matter of exposing it per-owner instead of discarding the
distinction.

**5. Confirmation — a name and a timestamp.** Once it lands: "Connected as you@gmail.com —
first session seen just now." Simple, and honest about what's actually known.

## 2. Tenant isolation — mostly a guarantee, with one real gap the review found

Per-device revoke is explicitly out of scope for now — not important yet. Most of what matters
for launch is already true: the codebase's architecture keeps tenant data walled off by
construction for the routes that matter most. One route doesn't, and that has to be fixed, not
just documented.

| Guarantee | Status | Where it lives |
|---|---|---|
| Every Google account gets exactly one identity (`google_sub`), one bearer token | already true | `get_or_create_token()`, `auth/store_firestore.py` |
| Every authenticated request is scoped to a single owner for its whole lifetime — *except* the owner-token itself, which sets `current_owner=None` and deliberately means "all data" (used by the OAuth route too) | true, with a documented exception | `current_owner` contextvar, set once by `MultiTokenAuthMiddleware`; `owner=None` path in `metrics/store.py` |
| `/api/*` reads/writes go through owner scope | already true today | Confirmed against `middleware.py`'s `protected_prefixes` |
| `GET /otlp/debug` is authenticated but **not owner-scoped** — it returns process-global counters, never filtered by `current_owner` | gap — must fix | `otlp/__init__.py`'s `_counts`/`_last_accepted_at`/`_recent_skipped` are module-level dicts, not keyed by owner; `routes/otlp.py`'s handler never reads `current_owner` at all |

This gap is exactly what "Test your connection" (§1.4) is built on, so it can't ship as a
documentation item — it's a required code change: key `_last_accepted_at` and `_recent_skipped`
by owner and filter in the route, or back the check with a separate owner-scoped query against
the metrics store. This also means the earlier claim that launch "requires no changes to
`mcp_server/otlp/`" doesn't hold — corrected in §4.

Once that's fixed, the remaining ask is still cheap but should be a **test that enumerates
routes**, not just a docstring — a docstring is exactly what the next person under time
pressure won't read. State the rule precisely: *every route that touches session/metrics/user
data must read `current_owner`; `None` means owner-token and is the intentional all-data path,
not a bug.*

## 3. Landing page — chill, streamlined, one job

Today's `/auth/login` page does sign-in, a manual-snippet fallback, and the download flow all
on one screen with a lot of explanatory prose. Streamlining this is a content and layout pass,
not a new subsystem:

- One clear action above the fold: sign in.
- After sign-in: the close-sessions notice, then the copy-paste command big and unmissable,
  then the test-connection panel directly beneath it — nothing else competing for attention.
- Everything else that exists today (manual env var snippets, Connectors-instead-of-script
  option, disclosure notes) moves behind a single "Advanced / manual setup" disclosure,
  collapsed by default — kept, not deleted, just no longer the first thing a new user reads.

## 4. Build list

| Piece | Status | Notes |
|---|---|---|
| `/setup/install` route serving a curl-able script | new | Wraps existing `local_setup.py` logic; adds OS detection and a short-lived token-exchange param instead of a token embedded in a downloaded file. |
| Same script also servable as a plain file (`-o install.sh`, no pipe) | free | Same route, no new logic — a normal GET response works as both a pipe target and a downloadable file. |
| Windows/PowerShell variant | new | Same patch logic, different shell. **Correction from review:** UA sniffing doesn't work here — `curl` sends a generic UA with no OS token, Windows users on Git Bash/WSL need the POSIX script not PowerShell, and UA is often absent behind proxies. Serving PowerShell into a `sh` pipe is the worst failure mode possible (cascading syntax errors on first run). Use the page's platform toggle as the only mechanism — default to what the browser reports (reliable), emit two visibly different commands (`curl … \| sh` vs `irm … \| iex`) via distinct routes or an explicit `?platform=` param, let the user override. |
| Short-lived install-token exchange | new | So the real bearer token never sits in shell history via a plaintext curl arg — a one-time code that exchanges for the real token server-side during install. **Correction from review:** "a one-time code" was underspecified — reuse the existing `issue_oauth_code`/`redeem_oauth_code` pattern in `auth/store_firestore.py` directly (hash-keyed storage, `expires_at`, transactional `consumed_at` for single-use) rather than inventing a new mechanism. Pin TTL to ≤5 minutes. Required error path: the script must handle a code that's already expired (the common case — user copies the command, gets pulled away, pastes it 20 minutes later) with a clear message, not a silent failure — "your install link expired, copy it again from the page." |
| `GET /otlp/debug` made owner-scoped | new — found in review | Currently global process state, not filtered by `current_owner` (see §2). Required before "Test your connection" can ship — without this fix the panel gives false positives across tenants and `recent_skipped` leaks other users' session data. This is the one item that does touch `mcp_server/otlp/`, contradicting the earlier "no changes needed there" line — corrected below. |
| "Close existing Claude Code sessions first" instruction | new | Copy only — a line on the page and/or printed by the install script itself, right after it finishes. |
| "Test your connection" panel | extends existing | Static "waiting" state plus a third "sent but not recognized" state; reads `GET /otlp/debug` on refresh, once that route is owner-scoped (row above). No polling loop needed for launch. |
| Landing page layout/copy pass | extends existing | `mcp_server/routes/auth.py`'s `auth_login` template — content and hierarchy, not new infrastructure. |
| Tenant-isolation invariant, enforced by a route-enumerating test | document + test | A short written rule plus a test that walks registered routes and checks each one touching session/metrics/user data reads `current_owner` — a docstring alone is exactly what gets skipped under time pressure. |
| Per-user token minting, ownership scoping | already built | No changes needed — this is the foundation the rest sits on. |
| Cursor support — research spike | new | A research subagent answering two *separate* questions, not one with a fallback — **correction from review:** OTel export and MCP config are different layers with different outcomes, not two paths to the same result. (1) Can Cursor point an OTel exporter at an arbitrary OTLP endpoint with arbitrary resource attributes — i.e. can we set `service.name=claude-code` ourselves the same way `local_setup.py` does today, since Claude Code doesn't emit that value natively either? This is the question that actually determines whether Cursor sessions can carry real token/cost telemetry into the dashboard. (2) Separately: does Cursor's MCP config surface support the same backup-and-merge pattern — this only gets tool-call visibility, not usage telemetry, per `routes/otlp.py`'s own docstring, and is a materially lesser outcome that shouldn't be described as "Cursor works" if that's all that lands. Output is a written finding per question, not code — verified against Cursor's real, current config surface (labeled with which Cursor version + install method was checked, since this varies) before anything is built, then tried hands-on by one friend as a check on a spike already believed to work, not as the sole validation. |

> **Correction from review:** this originally said nothing here touches `mcp_server/otlp/` —
> not quite true. Owner-scoping `GET /otlp/debug` (row above) does touch that module, because
> the gap it fixes lives there. Everything else holds: the ingestion pipeline, vendor-detection
> logic, and Firestore/auth store's actual matching rules are unchanged — this one route is a
> debug/status endpoint bolted on after the fact, not the pipeline itself, and fixing its
> scoping doesn't touch how telemetry is ingested or attributed.

## 5. Before this gets built — open questions

**Resolved — inspect-first path and close-sessions instruction.** Both settled this pass: the
inspect-first path is a collapsed, expandable block right under the primary command, same
page, no separate URL. The close-sessions instruction lives in both places — bold-italic page
copy above the install command, and printed again by the script itself after it runs — so it
shows up whether someone reads the page or only watches their terminal.

**Does the Cursor spike block launch, or ship after?** Recommend after — Cursor support is
additive and its research findings (see build list) aren't yet known to be feasible. Gating the
Claude Code launch on an unverified Cursor integration would trade a working thing for an
unproven one.

**Who validates the Cursor spike's findings before code gets written?** The plan calls for one
external user (a friend, on Cursor) to try it hands-on — worth deciding whether that happens
before or after the research subagent's findings are reviewed, so the friend isn't debugging a
spike that was already known to be broken.

---

Scope note: this plan changes delivery and visibility only. The OTLP ingestion pipeline,
vendor detection, and Firestore auth store are unchanged from the version verified working end
to end on 2026-08-25.
