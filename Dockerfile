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

# Static files for the mobile webapp (mcp_server/routes/webapp.py resolves
# these relative to the repo root at runtime, not through the Python
# package install above).
COPY webapp/ ./webapp/

# Deterministic demo dataset (scripts/seed_demo_db.py) — only used when
# the deploy sets DEMO_SEED_SRC + METRICS_DB_PATH (see README/docs); a
# local dev run of this image ignores both and behaves as before.
COPY demo/ ./demo/

ENV HOST=0.0.0.0
EXPOSE 8080
CMD [".venv/bin/python", "-m", "mcp_server.server"]
