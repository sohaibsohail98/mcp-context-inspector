# Cloud Run deployment of the MCP server. Build from the repo root:
#   docker build -t <tag> .

FROM python:3.13-slim

WORKDIR /app

# `pip install .` resolves dependencies fresh from PyPI on every build,
# ignoring uv.lock entirely — a transitive dependency picking up an
# unpinned point release between builds (e.g. google-api-core 2.34.0 ->
# 2.35.0, which broke Firestore's database-id path templating for ids
# containing parentheses like the default database's "(default)") can
# silently break the deployed image with zero corresponding change in
# this repo. Installing from the lockfile makes every build byte-for-
# byte reproducible until uv.lock is deliberately updated.
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock ./
COPY mcp_server/ ./mcp_server/
COPY metrics/ ./metrics/
COPY mci_common/ ./mci_common/
RUN uv sync --frozen --no-dev

# Static frontends resolved relative to the repo root at runtime (not
# through the Python package install above): the mobile webapp
# (routes/webapp.py), the /docs site (routes/docs.py), and the
# /auth/login sign-in / dashboard SPA (routes/auth.py serves
# dashboard/index.html + dashboard.css + dashboard.js).
COPY webapp/ ./webapp/
COPY docs-site/ ./docs-site/
COPY dashboard/ ./dashboard/

# Deterministic demo dataset (scripts/seed_demo_db.py) — only used when
# the deploy sets DEMO_SEED_SRC + METRICS_DB_PATH (see README/docs); a
# local dev run of this image ignores both and behaves as before.
COPY demo/ ./demo/

ENV HOST=0.0.0.0
EXPOSE 8080

# /health and its alias /ping both return {"status":"ok"} unauthenticated
# (mcp_server/routes/api.py). Cloud Run ignores Dockerfile HEALTHCHECK and
# runs its own probe, but `docker`/`podman` and some registry build
# pipelines honour this one. Shell form so it follows $PORT (the server
# binds $PORT, then $MCP_SERVER_PORT, then 8787; the deploy sets
# PORT=8080 to match EXPOSE).
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD .venv/bin/python -c "import os,urllib.request,sys; p=os.environ.get('PORT') or os.environ.get('MCP_SERVER_PORT') or '8787'; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/health', timeout=2).status==200 else 1)"

CMD [".venv/bin/python", "-m", "mcp_server.server"]
