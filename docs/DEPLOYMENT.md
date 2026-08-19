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

GitHub Actions deploy workflow (Workload Identity Federation) is not
built yet.
