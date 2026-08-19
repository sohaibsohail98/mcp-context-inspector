# Cloud Run deployment of the MCP server. Build from the repo root:
#   docker build -t <tag> .

FROM python:3.13-slim

WORKDIR /app

COPY pyproject.toml ./
COPY mcp_server/ ./mcp_server/
COPY metrics/ ./metrics/
COPY mci_common/ ./mci_common/
RUN pip install --no-cache-dir .

# Deterministic demo dataset (scripts/seed_demo_db.py) — only used when
# the deploy sets DEMO_SEED_SRC + METRICS_DB_PATH (see README/docs); a
# local dev run of this image ignores both and behaves as before.
COPY demo/ ./demo/

ENV HOST=0.0.0.0
EXPOSE 8080
CMD ["python", "-m", "mcp_server.server"]
