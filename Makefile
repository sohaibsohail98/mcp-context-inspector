# Demo capture pipeline: reseeds the deterministic demo database, starts
# the server in demo mode, runs the Playwright recording, converts it to
# the two shipped output formats, then tears the server down. See
# scripts/demo_capture.py, scripts/encode_demo_take.sh, and
# docs/internal/demo-brief.md for the full choreography.
#
# Everything here is dev/CI tooling, never part of the deployed image
# (see pyproject.toml: playwright is a dev-only dependency, ffmpeg is a
# system binary this target assumes is already on PATH).
#
# `make demo` produces the single shipped docs/demo.mp4 / docs/demo.gif,
# playing whichever demo_reveal.js choreography REVEAL_MODE names. After
# reviewing all four terminal-to-dashboard candidates (see
# docs/internal/demo-takes.md and -v2.md), guided_tour is the sole
# shipped concept: `make demo` (CUT=full, the default) renders
# docs/demo.mp4 for LinkedIn, `make demo CUT=short` renders the tighter
# docs/demo.gif for the README. CUT only affects guided_tour; the other
# three modes ignore it and are kept only for `make demo-candidates`.
# `make demo-candidates` renders one take per entry in REVEAL_MODES (each
# with its own sample prompt and typing speed, see
# scripts/demo_candidate_params.sh) into the gitignored docs/_scratch/ so
# they can be compared side by side.

.PHONY: demo demo-candidates

DEMO_DB := demo/metrics.db
DEMO_PORT := 8799
DEMO_HOST := 127.0.0.1
DEMO_SERVER_URL := http://$(DEMO_HOST):$(DEMO_PORT)
RAW_DIR := docs/_raw
SCRATCH_DIR := docs/_scratch
REVEAL_MODE ?= guided_tour
CUT ?= full
REVEAL_MODES := guided_tour cost_reveal surprise multi_turn

# Per reveal-mode sample prompt and dashboard-half duration are looked
# up via scripts/demo_candidate_params.sh rather than a Make define/call:
# a shell `case` embedded in a Make define gets flattened onto one line
# and loses its own statement separators (tried that first, hit a syntax
# error), a real script file does not have that problem.
PARAMS_SCRIPT := scripts/demo_candidate_params.sh

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
	PROMPT=$$(sh $(PARAMS_SCRIPT) prompt $(REVEAL_MODE) $(CUT)); \
	DASHBOARD_SECONDS=$$(sh $(PARAMS_SCRIPT) seconds $(REVEAL_MODE) $(CUT)); \
	echo "==> Recording reveal mode '$(REVEAL_MODE)' cut '$(CUT)' (Playwright + Chromium)"; \
	CAPTURE_OUTPUT=$$(.venv/bin/python -m scripts.demo_capture --server-url $(DEMO_SERVER_URL) --out-dir $(RAW_DIR) --reveal-mode $(REVEAL_MODE) --cut $(CUT) --prompt "$$PROMPT"); \
	echo "$$CAPTURE_OUTPUT"; \
	TERMINAL_VIDEO=$$(echo "$$CAPTURE_OUTPUT" | sed -n 's/^Wrote terminal capture to //p'); \
	DASHBOARD_VIDEO=$$(echo "$$CAPTURE_OUTPUT" | sed -n 's/^Wrote dashboard capture to //p'); \
	TRIM_START=$$(echo "$$CAPTURE_OUTPUT" | sed -n 's/^TRIM_START_SECONDS=//p'); \
	kill $$SERVER_PID 2>/dev/null || true; \
	trap - EXIT; \
	echo "==> Encoding docs/demo.mp4 and docs/demo.gif"; \
	sh scripts/encode_demo_take.sh "$$TERMINAL_VIDEO" "$$DASHBOARD_VIDEO" "$$TRIM_START" "$$DASHBOARD_SECONDS" docs/demo.mp4 docs/demo.gif $(RAW_DIR)/work; \
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
	for mode in $(REVEAL_MODES); do \
		PROMPT=$$(sh $(PARAMS_SCRIPT) prompt $$mode); \
		DASHBOARD_SECONDS=$$(sh $(PARAMS_SCRIPT) seconds $$mode); \
		echo "==> Recording candidate '$$mode' (Playwright + Chromium)"; \
		CAPTURE_OUTPUT=$$(.venv/bin/python -m scripts.demo_capture --server-url $(DEMO_SERVER_URL) --out-dir $(RAW_DIR)/$$mode --reveal-mode $$mode --prompt "$$PROMPT"); \
		echo "$$CAPTURE_OUTPUT"; \
		TERMINAL_VIDEO=$$(echo "$$CAPTURE_OUTPUT" | sed -n 's/^Wrote terminal capture to //p'); \
		DASHBOARD_VIDEO=$$(echo "$$CAPTURE_OUTPUT" | sed -n 's/^Wrote dashboard capture to //p'); \
		TRIM_START=$$(echo "$$CAPTURE_OUTPUT" | sed -n 's/^TRIM_START_SECONDS=//p'); \
		sh scripts/encode_demo_take.sh "$$TERMINAL_VIDEO" "$$DASHBOARD_VIDEO" "$$TRIM_START" "$$DASHBOARD_SECONDS" $(SCRATCH_DIR)/$$mode.mp4 $(SCRATCH_DIR)/$$mode.gif $(RAW_DIR)/$$mode/work; \
		echo "$(SCRATCH_DIR)/$$mode.mp4: $$(du -h $(SCRATCH_DIR)/$$mode.mp4 | cut -f1), $$(ffprobe -v error -show_entries format=duration -of csv=p=0 $(SCRATCH_DIR)/$$mode.mp4)s"; \
	done; \
	kill $$SERVER_PID 2>/dev/null || true; \
	trap - EXIT; \
	rm -rf $(RAW_DIR); \
	echo "==> Done. Candidates in $(SCRATCH_DIR)/ (gitignored, not shipped)."
