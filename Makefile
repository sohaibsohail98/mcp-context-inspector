# Demo capture pipeline: reseeds the deterministic demo database, starts
# the server in demo mode, runs the Playwright recording, converts it to
# the two shipped output formats, then tears the server down. See
# scripts/demo_capture.py and demo-brief.md for the full choreography.
#
# Everything here is dev/CI tooling, never part of the deployed image
# (see pyproject.toml: playwright is a dev-only dependency, ffmpeg is a
# system binary this target assumes is already on PATH).

.PHONY: demo

DEMO_DB := demo/metrics.db
DEMO_PORT := 8799
DEMO_HOST := 127.0.0.1
DEMO_SERVER_URL := http://$(DEMO_HOST):$(DEMO_PORT)
RAW_DIR := docs/_raw
PALETTE := $(RAW_DIR)/palette.png

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
	echo "==> Waiting for the server to come up"; \
	for i in $$(seq 1 50); do \
		curl -sf $(DEMO_SERVER_URL)/health > /dev/null 2>&1 && break; \
		sleep 0.2; \
	done; \
	curl -sf $(DEMO_SERVER_URL)/health > /dev/null || (echo "server never came up" && exit 1); \
	echo "==> Recording (Playwright + Chromium)"; \
	CAPTURE_OUTPUT=$$(.venv/bin/python -m scripts.demo_capture --server-url $(DEMO_SERVER_URL) --out-dir $(RAW_DIR)); \
	echo "$$CAPTURE_OUTPUT"; \
	RAW_VIDEO=$$(echo "$$CAPTURE_OUTPUT" | sed -n 's/^Wrote raw capture to //p'); \
	TRIM_START=$$(echo "$$CAPTURE_OUTPUT" | sed -n 's/^TRIM_START_SECONDS=//p'); \
	kill $$SERVER_PID 2>/dev/null || true; \
	trap - EXIT; \
	echo "==> Encoding docs/demo.mp4 (square, letterboxed onto #17150f)"; \
	ffmpeg -y -ss $$TRIM_START -t 8 -i "$$RAW_VIDEO" \
		-vf "scale=1080:1080:force_original_aspect_ratio=decrease,pad=1080:1080:(ow-iw)/2:(oh-ih)/2:color=0x17150f" \
		-c:v libx264 -pix_fmt yuv420p -movflags +faststart docs/demo.mp4; \
	echo "==> Building docs/demo.gif (two pass palette, 900px wide, 15fps)"; \
	ffmpeg -y -ss $$TRIM_START -t 8 -i "$$RAW_VIDEO" -vf "fps=15,scale=900:-1:flags=lanczos,palettegen=stats_mode=diff" -update 1 $(PALETTE); \
	ffmpeg -y -ss $$TRIM_START -t 8 -i "$$RAW_VIDEO" -i $(PALETTE) \
		-lavfi "fps=15,scale=900:-1:flags=lanczos [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=3" \
		-t 8 docs/demo.gif; \
	rm -rf $(RAW_DIR); \
	echo "==> Done"; \
	echo "docs/demo.mp4: $$(du -h docs/demo.mp4 | cut -f1), $$(ffprobe -v error -show_entries format=duration -of csv=p=0 docs/demo.mp4)s"; \
	echo "docs/demo.gif: $$(du -h docs/demo.gif | cut -f1)"
