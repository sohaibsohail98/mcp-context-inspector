# Lucid architecture diagram prompt (ctxwindow)

Paste this into Lucid's AI diagram generator (or use it as a manual build spec) to
produce the architecture board for the interview/demo.

---

Create a cloud architecture diagram for a system called ctxwindow, an MCP (Model
Context Protocol) server with a web dashboard, deployed on Google Cloud Platform
fronted by Cloudflare. Lay it out left to right: client layer, edge/network layer,
compute layer, data layer, identity layer, and CI/CD layer as a separate swimlane
underneath. Use AWS/GCP-style icon shapes where available, otherwise labeled
rectangles. Use arrows to show request direction, and dashed arrows for
control-plane/deploy-time relationships (as opposed to solid arrows for runtime
request traffic).

## Client layer (left)

Three client types, each with an arrow pointing right into the edge layer:

1. Claude Code (CLI), connecting over the MCP protocol (JSON-RPC over HTTP with
   Server-Sent Events for the server-to-client stream) with a bearer token in the
   Authorization header.
2. claude.ai Connectors / Cursor / GitHub Copilot, connecting via full OAuth 2.1
   with PKCE (no client secret, public client).
3. A browser, hitting the web dashboard directly (Google sign-in, then the Context
   Window Explorer UI).

## Edge layer

A single box: "Cloudflare Worker (reverse proxy)", labeled with:
- Domain: ctxwindow.uk (custom domain, owned)
- Adds security headers on every response: Content-Security-Policy, X-Frame-Options,
  X-Content-Type-Options, Referrer-Policy
- Pure pass-through proxy to the Cloud Run origin, no logic beyond header injection
- Also reachable at a workers.dev subdomain (legacy bookmarks)

Arrow from this box down into the compute layer, labeled "forwards every request
unchanged (Host header re-derived from origin, not from the incoming request)".

## Compute layer

One box: "Cloud Run service: mcp-context-inspector"
- Single container, Python/Starlette (ASGI)
- Autoscales 0 to 3 instances, concurrency 80 per instance
- Runs as a dedicated service account with least-privilege IAM (Firestore user role
  only, nothing else)
- Public ingress, but access control is enforced inside the app, not by Cloud Run IAM

Inside this box (or as a sub-diagram), show four internal modules as smaller nested
boxes with arrows between them:

1. **MultiTokenAuthMiddleware** — the entry gate for /mcp, /api/, /otlp, /setup.
   Accepts three token types: the owner's shared token, a per-user token minted at
   sign-in, or (only in demo mode) a fixed demo token. Also carves out an
   unauthenticated path for the MCP discovery handshake itself (initialize,
   tools/list, ping, etc, but never tools/call or resources/read) so registry
   crawlers can index the tool catalogue without credentials.
2. **OAuth 2.1 authorization server** (mcp_server/routes/oauth.py) — implements
   RFC 9728 (protected resource metadata), RFC 8414 (authorization server
   metadata), RFC 7591 (dynamic client registration), and PKCE. This server acts
   as BOTH the OAuth resource server (the /mcp endpoint) and the authorization
   server; there's no external identity provider beyond Google sign-in, which it
   already verifies itself.
3. **Google Identity verification** — verifies the Google-issued ID token
   (signature, audience, issuer) via google-auth, extracting the account's sub and
   email. This is the actual authentication step underneath both the dashboard
   login and the OAuth authorize/consent flow.
4. **MCP tool layer** — 8 read/write tools exposed over the MCP protocol
   (get_session_metrics, get_token_breakdown, get_tool_metrics, get_agent_trace,
   get_cost_estimate, get_recent_sessions, get_context_timeline, record_session),
   each with explicit MCP annotations (readOnlyHint, destructiveHint,
   idempotentHint, openWorldHint) so a client can auto-approve reads and prompt
   only for the one write tool.

## Data layer

One box: "Firestore (Native mode, us-central1)"
- Stores session metrics, per-turn token/cost breakdowns, and per-user auth
  records (device tokens, OAuth-issued tokens)
- Single project-wide default database, accessed via the Cloud Run service
  account's datastore.user role
- A separate Secret Manager secret holds the shared owner bearer token, injected
  into the Cloud Run container as an environment variable at deploy time (never
  baked into the image)

## OAuth flow (draw as a separate numbered sequence, either inline or as a second
diagram frame)

1. MCP client hits /mcp with no or an invalid token.
2. Server responds 401 with a WWW-Authenticate header pointing at
   /.well-known/oauth-protected-resource/mcp.
3. Client fetches that, discovers this server is also its own authorization
   server, then fetches /.well-known/oauth-authorization-server for the real
   endpoints.
4. Client POSTs /oauth/register once (RFC 7591) and gets back a client_id, no
   manual setup, no client secret since it's a public client protected by PKCE.
5. Client opens /oauth/authorize in a browser; the user signs in with Google
   (reusing the same identity verification as the dashboard); server redirects
   back with a one-time authorization code.
6. Client exchanges the code plus PKCE verifier at /oauth/token for an ordinary
   bearer access token.
7. That bearer token is now indistinguishable, from the auth middleware's point
   of view, from a token minted through the plain sign-in flow: same validity
   check, same per-user scoping.

## CI/CD swimlane (draw underneath the main diagram, connected by dashed arrows
into the compute and data layers above)

Left to right:

1. **GitHub Actions** (push to main, or PR)
2. **Workload Identity Federation** — GitHub's OIDC token is exchanged for a
   short-lived GCP credential impersonating a dedicated deploy service account.
   No stored GCP keys in GitHub secrets.
3. **Terraform** (infrastructure/terraform) — plan runs on every PR (with tflint
   and checkov as policy/lint gates), apply runs on merge to main. Terraform owns
   the Cloud Run service definition, IAM bindings, Artifact Registry repository,
   Firestore database, Secret Manager secret container, and the Cloudflare custom
   domain mapping. State lives in a versioned, access-logged GCS bucket.
4. **Docker build + push** — image built and pushed to Artifact Registry, tagged
   with the commit SHA.
5. **Cloud Run deploy** (via Terraform apply with the new image tag as a
   variable) — moves 100% traffic to the new revision.
6. **Cloudflare Worker deploy** (wrangler, separate step) — deploys the edge
   proxy independently of the origin service.
7. **Smoke test** — curls /health on both the direct Cloud Run URL and through
   the Cloudflare domain before the pipeline is considered green.

## Style notes

- Dark background, one accent colour for "your infrastructure" boxes (Cloud Run,
  Firestore, the Cloudflare Worker, Terraform) versus a neutral colour for
  external actors (clients, Google's identity servers, GitHub Actions runners).
- Keep the OAuth sequence visually distinct from the main request-flow diagram,
  either as a separate frame/page or a clearly boxed-off region, so the two
  don't compete for attention when presenting.
- Label every arrow with the protocol (HTTPS, JSON-RPC/SSE, gRPC where relevant)
  rather than leaving them bare.
