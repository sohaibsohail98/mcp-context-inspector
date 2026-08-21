# Deployment

Stub, full deploy walkthrough not written yet. What's true today:

- Deployed to Cloud Run (`us-central1`), `--min-instances 0
  --max-instances 3`, no CPU-always-allocated, an Artifact Registry
  cleanup policy keeping the 3 most recent images.
- The public demo deployment runs `STORAGE_BACKEND=sqlite`, seeded from a
  deterministic demo dataset (`scripts/seed_demo_db.py` → `demo/metrics.db`,
  baked into the image) via `DEMO_SEED_SRC` + `METRICS_DB_PATH` env vars.
  Writes behind Google sign-in land in an ephemeral copy that resets on
  cold start. This is the right tradeoff for a free public demo (no
  standing storage cost, no real user data to protect), not a limitation
  of the storage layer itself — a real deployment sets
  `STORAGE_BACKEND=firestore` or `STORAGE_BACKEND=dynamodb` instead; see
  "Firestore setup" below.
- Warmed by 2 Cloud Scheduler jobs hitting `/health` every 10 minutes.
- Fronted by a Cloudflare Worker (`cloudflare-proxy/`, deployed with
  `npx wrangler deploy`), a transparent fetch-and-stream reverse proxy
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
  `mcp-context-inspector-1097847824883.us-central1.run.app`), not the
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
  Host header`. CORS/preflight and unauthenticated (401) requests look
  fine, so a plain unauthenticated curl smoke test won't catch it; test
  with a real bearer token.

GitHub Actions deploy workflow (Workload Identity Federation) is live.
Pushes to `main` touching `mcp_server/**` (or other deploy-relevant
paths) auto-deploy to the Cloud Run service and Cloudflare Worker above.

## Firestore setup

For a real deployment where signed-in users' data needs to survive a
Cloud Run cold start, set `STORAGE_BACKEND=firestore` on both the Cloud
Run service and anywhere `mcp_server/auth/store.py` runs (the two stores
share the same env var and must be switched together — see both
dispatchers' docstrings). This backs both the session/metrics data
(`metrics/store_firestore.py`) and the per-user auth token store
(`mcp_server/auth/store_firestore.py`) with real, durable Firestore
collections, so a cold start no longer resets a signed-in user's
sessions or silently invalidates their token.

- Uses `google.cloud.firestore.Client()` via Application Default
  Credentials — no key file needed. Grant Cloud Run's built-in service
  account the `roles/datastore.user` IAM role on the GCP project:
  `gcloud projects add-iam-policy-binding <project> --member=serviceAccount:<cloud-run-sa> --role=roles/datastore.user`.
- `get_recent_sessions`'s owner-scoped query
  (`sessions.where("owner", "==", owner).order_by("timestamp", DESCENDING)`)
  needs a composite index on `(owner ASC, timestamp DESC)` on the
  `sessions` collection. Firestore's own error on the first unindexed
  query includes a console link to create it, but don't rely on that
  happening live in prod: create the index ahead of time via the Firestore
  console (Indexes → Composite → Add Index → collection `sessions`,
  fields `owner` Ascending + `timestamp` Descending) or `gcloud firestore
  indexes composite create`.
- Local testing against the real Firestore emulator (`gcloud emulators
  firestore start`, or `firebase emulators:start` if using the Firebase
  CLI) rather than a real GCP project: set `FIRESTORE_EMULATOR_HOST`
  (e.g. `localhost:8080`) before starting the server or running
  `uv run pytest` — the client library picks this up automatically, no
  code changes needed. `tests/test_metrics_store_firestore.py` and
  `tests/test_auth_store_firestore.py` skip cleanly if the emulator isn't
  reachable.

## Running it locally

For local development and testing before pushing (see the README's
"Run it locally" section for the exact commands): set
`STORAGE_BACKEND=sqlite`, point `METRICS_DB_PATH` at a scratch file so
you don't touch `data/metrics.db`, set a fixed `MCP_AUTH_TOKEN` so it
doesn't rotate on every restart, and optionally set
`GOOGLE_OAUTH_CLIENT_ID` (with `http://localhost:8787` as an authorized
JavaScript origin on that client) to test the real Google sign-in
button. Without it, `/auth/login` still renders and works with the
owner token alone.
