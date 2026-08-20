# Deployment

Stub — full deploy walkthrough not written yet. What's true today:

- Deployed to Cloud Run (`us-central1`), `--min-instances 0
  --max-instances 3`, no CPU-always-allocated, an Artifact Registry
  cleanup policy keeping the 3 most recent images.
- `STORAGE_BACKEND=sqlite`, seeded from a deterministic demo dataset
  (`scripts/seed_demo_db.py` → `demo/metrics.db`, baked into the image)
  via `DEMO_SEED_SRC` + `METRICS_DB_PATH` env vars — writes behind
  Google sign-in land in an ephemeral copy that resets on cold start,
  documented tradeoff, not a bug.
- Warmed by 2 Cloud Scheduler jobs hitting `/health` every 10 minutes.
- Fronted by a Cloudflare Worker (`cloudflare-proxy/`, deployed with
  `npx wrangler deploy`) — a transparent fetch-and-stream reverse proxy
  giving this service a short, permanent public URL
  (`https://mcp-inspector.sohaibsohail.workers.dev`) instead of the raw
  Cloud Run `.run.app` one. Cloud Run's `CHAT_UI_ORIGIN` env var (CORS
  allowlist) is a comma-separated list so both the Worker origin and any
  other real chat-UI origins stay allowed simultaneously.
- Cloud Run's `MCP_ALLOWED_HOSTS` env var (comma-separated, same
  convention as `CHAT_UI_ORIGIN`) allowlists the `Host` header value(s)
  the MCP SDK's DNS-rebinding protection will accept in production, in
  addition to the always-allowed loopback entries. The Cloudflare Worker
  proxy strips the inbound Host header and lets `fetch()` re-derive it
  from `env.ORIGIN` (`cloudflare-proxy/wrangler.toml`), so the Host
  header Cloud Run's container actually sees is that literal
  `env.ORIGIN` hostname (currently
  `mcp-context-inspector-1097847824883.us-central1.run.app`) — not the
  `mcp-inspector.workers.dev` proxy hostname, and not necessarily the
  hash-based `*.a.run.app` hostname `gcloud run services describe`
  reports (Cloud Run services can have both a project-number and a
  hash-based URL; only whichever one is literally in `env.ORIGIN`
  matters here). Currently set to both, since either could plausibly be
  the live value: run
  `gcloud run services update mcp-context-inspector --region=us-central1 --update-env-vars='^@@^MCP_ALLOWED_HOSTS=<host1>,<host2>'`
  (custom `^@@^` delimiter needed since the value itself contains
  commas) if the Cloud Run URL or `cloudflare-proxy/wrangler.toml`'s
  `ORIGIN` ever changes. Getting this wrong manifests as every
  authenticated production request to `/mcp` failing with `421 Invalid
  Host header` — CORS/preflight and unauthenticated (401) requests look
  fine, so a plain unauthenticated curl smoke test won't catch it; test
  with a real bearer token.

GitHub Actions deploy workflow (Workload Identity Federation) is live —
pushes to `main` touching `mcp_server/**` (or other deploy-relevant
paths) auto-deploy to the Cloud Run service and Cloudflare Worker above.
