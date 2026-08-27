# Demo capture pipeline: reseeds the deterministic demo database, starts
# the server in demo mode, runs the Playwright recording, converts it to
# the two shipped output formats, then tears the server down. See
# scripts/demo_capture.py, scripts/encode_demo_take.sh, and demo-brief.md
# for the full choreography.
#
# Everything here is dev/CI tooling, never part of the deployed image
# (see pyproject.toml: playwright is a dev-only dependency, ffmpeg is a
# system binary this target assumes is already on PATH).
#
# `make demo` produces the single shipped docs/demo.mp4 / docs/demo.gif,
# expanding whichever block VARIANT names (default: tool_result, the
# take picked as the best of the candidates, see
# docs/internal/demo-takes.md for why). `make demo-candidates` renders
# one take per entry in EXPAND_VARIANTS (scripts/demo_capture.py) into
# the gitignored docs/_scratch/ so they can be compared side by side
# before choosing.

.PHONY: demo demo-candidates

DEMO_DB := demo/metrics.db
DEMO_PORT := 8799
DEMO_HOST := 127.0.0.1
DEMO_SERVER_URL := http://$(DEMO_HOST):$(DEMO_PORT)
RAW_DIR := docs/_raw
SCRATCH_DIR := docs/_scratch
VARIANT ?= tool_result
VARIANTS := tools system tool_result

demo:
	@set -e; \
	mkdir -p docs; \
	echo "==> Reseeding $(DEMO_DB) (deterministic, see scripts/seed_demo_db.py)"; \
	.venv/bin/python -m scripts.seed_demo_db --out $(DEMO_DB); \
	echo "==> Starting server in demo mode on $(DEMO_SERVER_URL)"; \
	CTXWINDOW_DEMO_MODE=1 METRICS_DB_PATH=$(DEMO_DB) HOST=$(DEMO_HOST) PORT=$(DEMO_PORT) \
		GOOGLE_OAUTH_CLIENT_ID=demo-client-id MCP_AUTH_TOKEN=demo-owner-token \
		.venv/bin/python -m mcp_server.server & \
	SERVER_PID=$$!; \
	trap 'kill $$SERVER_PID 2>/dev/null || true' EXIT; \
	for i in $$(seq 1 50); do \
		curl -sf $(DEMO_SERVER_URL)/health > /dev/null 2>&1 && break; \
		sleep 0.2; \
	done; \
	curl -sf $(DEMO_SERVER_URL)/health > /dev/null || (echo "server never came up" && exit 1); \
	echo "==> Recording variant '$(VARIANT)' (Playwright + Chromium)"; \
	CAPTURE_OUTPUT=$$(.venv/bin/python -m scripts.demo_capture --server-url $(DEMO_SERVER_URL) --out-dir $(RAW_DIR) --variant $(VARIANT)); \
	echo "$$CAPTURE_OUTPUT"; \
	RAW_VIDEO=$$(echo "$$CAPTURE_OUTPUT" | sed -n 's/^Wrote raw capture to //p'); \
	TRIM_START=$$(echo "$$CAPTURE_OUTPUT" | sed -n 's/^TRIM_START_SECONDS=//p'); \
	kill $$SERVER_PID 2>/dev/null || true; \
	trap - EXIT; \
	echo "==> Encoding docs/demo.mp4 and docs/demo.gif"; \
	sh scripts/encode_demo_take.sh "$$RAW_VIDEO" "$$TRIM_START" docs/demo.mp4 docs/demo.gif $(RAW_DIR)/palette.png; \
	rm -rf $(RAW_DIR); \
	echo "==> Done"; \
	echo "docs/demo.mp4: $$(du -h docs/demo.mp4 | cut -f1), $$(ffprobe -v error -show_entries format=duration -of csv=p=0 docs/demo.mp4)s"; \
	echo "docs/demo.gif: $$(du -h docs/demo.gif | cut -f1)"

demo-candidates:
	@set -e; \
	mkdir -p docs "$(SCRATCH_DIR)"; \
	echo "==> Reseeding $(DEMO_DB) (deterministic, see scripts/seed_demo_db.py)"; \
	.venv/bin/python -m scripts.seed_demo_db --out $(DEMO_DB); \
	echo "==> Starting server in demo mode on $(DEMO_SERVER_URL)"; \
	CTXWINDOW_DEMO_MODE=1 METRICS_DB_PATH=$(DEMO_DB) HOST=$(DEMO_HOST) PORT=$(DEMO_PORT) \
		GOOGLE_OAUTH_CLIENT_ID=demo-client-id MCP_AUTH_TOKEN=demo-owner-token \
		.venv/bin/python -m mcp_server.server & \
	SERVER_PID=$$!; \
	trap 'kill $$SERVER_PID 2>/dev/null || true' EXIT; \
	for i in $$(seq 1 50); do \
		curl -sf $(DEMO_SERVER_URL)/health > /dev/null 2>&1 && break; \
		sleep 0.2; \
	done; \
	curl -sf $(DEMO_SERVER_URL)/health > /dev/null || (echo "server never came up" && exit 1); \
	for variant in $(VARIANTS); do \
		echo "==> Recording candidate '$$variant' (Playwright + Chromium)"; \
		CAPTURE_OUTPUT=$$(.venv/bin/python -m scripts.demo_capture --server-url $(DEMO_SERVER_URL) --out-dir $(RAW_DIR)/$$variant --variant $$variant); \
		echo "$$CAPTURE_OUTPUT"; \
		RAW_VIDEO=$$(echo "$$CAPTURE_OUTPUT" | sed -n 's/^Wrote raw capture to //p'); \
		TRIM_START=$$(echo "$$CAPTURE_OUTPUT" | sed -n 's/^TRIM_START_SECONDS=//p'); \
		sh scripts/encode_demo_take.sh "$$RAW_VIDEO" "$$TRIM_START" $(SCRATCH_DIR)/$$variant.mp4 $(SCRATCH_DIR)/$$variant.gif $(RAW_DIR)/$$variant/palette.png; \
		echo "$(SCRATCH_DIR)/$$variant.mp4: $$(du -h $(SCRATCH_DIR)/$$variant.mp4 | cut -f1)"; \
	done; \
	kill $$SERVER_PID 2>/dev/null || true; \
	trap - EXIT; \
	rm -rf $(RAW_DIR); \
	echo "==> Done. Candidates in $(SCRATCH_DIR)/ (gitignored, not shipped)."
