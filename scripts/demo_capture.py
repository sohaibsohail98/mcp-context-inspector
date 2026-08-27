"""Records the eight second Context Window Explorer demo used for
docs/demo.mp4 and docs/demo.gif. Playwright drives a real Chromium
against a real, locally running instance of this server, in demo mode,
against the deterministic seeded database (see seed_demo_db.py), so the
recording is reproducible: same DOM, same timings, every run.

Deliberately does not sign in via Google. auth.py's rehydrateFromStorage()
already trusts a token found in localStorage on first load, so an
addInitScript injecting CTXWINDOW's demo token there before the first
navigation reaches the dashboard exactly the way a returning real user's
browser would, without a real (and non-deterministic) OAuth round trip.

Run via `make demo`, which also reseeds the database, starts the server
in demo mode, and converts the recorded video (see the Makefile for the
ffmpeg invocation). Running this file directly assumes a server is
already up at DEMO_SERVER_URL with CTXWINDOW_DEMO_MODE=1.
"""

import argparse
import asyncio
import os
import shutil
import time
from pathlib import Path

from playwright.async_api import async_playwright

from mcp_server.middleware import DEMO_TOKEN

REPO_ROOT = Path(__file__).parent.parent
DEFAULT_SERVER_URL = os.environ.get("DEMO_SERVER_URL", "http://127.0.0.1:8787")
DEFAULT_OUT_DIR = REPO_ROOT / "docs" / "_raw"

VIEWPORT = {"width": 1100, "height": 720}

# Candidate blocks to expand in the 4.5s-6.5s reveal step, each a real
# illustration of "something consuming context you wouldn't have
# guessed" (see seed_demo_db.py, which is the only place these three
# blocks carry real content rather than the placeholder). Selected via
# --variant; used to produce multiple candidate takes for comparison,
# see docs/internal/demo-takes.md for how the final one was chosen.
EXPAND_VARIANTS = {
    "tools": "Tool specs",
    "system": "System prompt",
    "tool_result": "Tool result: list_services",
}

# A small circle rather than a stock arrow PNG, styled to sit inside the
# warm dark palette (--accent) so it reads as part of the product, not a
# recording artefact glued on top.
_FAKE_CURSOR_INIT_SCRIPT = """
(function () {
  var cursor = document.createElement("div");
  cursor.id = "__demo_fake_cursor";
  cursor.style.cssText = [
    "position: fixed", "z-index: 2147483647", "width: 14px", "height: 14px",
    "border-radius: 999px", "background: #6cbfa4", "box-shadow: 0 0 0 4px rgba(108,191,164,0.25)",
    "pointer-events: none", "transform: translate(-50%, -50%)", "left: -100px", "top: -100px",
    "transition: left 20ms linear, top 20ms linear",
  ].join(";");
  window.addEventListener("DOMContentLoaded", function () {
    document.body.appendChild(cursor);
  });
  document.addEventListener("mousemove", function (e) {
    cursor.style.left = e.clientX + "px";
    cursor.style.top = e.clientY + "px";
  });
})();
"""


def _session_injection_script(token, email):
    # String-format rather than an f-string with braces: keeps the JS's
    # own braces from needing doubling, same trap test_dashboard_js_syntax.py
    # exists to catch on the Python side of auth.py.
    return (
        "(function () {"
        "  window.localStorage.setItem('mci_token', '%s');"
        "  window.localStorage.setItem('mci_email', '%s');"
        "})();"
    ) % (token, email)


async def _click_with_beats(page, locator):
    """Instantaneous Playwright actions read as a rendering glitch on
    playback, not a real cursor; the pre/post beats are what makes this
    look captured rather than generated (see the brief)."""
    box = await locator.bounding_box()
    if box is None:
        raise RuntimeError("element not visible, cannot click for the demo recording")
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    await page.mouse.move(x, y, steps=25)
    await page.wait_for_timeout(300)
    await locator.click()
    await page.wait_for_timeout(400)


async def capture(server_url, out_dir, headless=True, expand_label=EXPAND_VARIANTS["tools"]):
    """Returns (video_path, trim_start_seconds). Playwright's recordVideo
    starts from context creation, before the session is selected, so the
    raw file's opening frames show the "Select a session" empty state
    (see renderDashboardShell). Rather than script the sign-in/loading
    moment away with a hack, the choreography's real "first frame must
    already be interesting" requirement is met downstream: the Makefile's
    ffmpeg pass trims the encode to start at trim_start_seconds, right
    after the first session is selected, so the output's frame 0 is the
    already-selected dashboard, matching the brief exactly."""
    out_dir.mkdir(parents=True, exist_ok=True)
    video_dir = out_dir / "video"
    if video_dir.exists():
        shutil.rmtree(video_dir)
    video_dir.mkdir(parents=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        recording_started_at = time.monotonic()
        context = await browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=2,
            record_video_dir=str(video_dir),
            record_video_size=VIEWPORT,
        )
        await context.add_init_script(_FAKE_CURSOR_INIT_SCRIPT)
        await context.add_init_script(_session_injection_script(DEMO_TOKEN, "demo@ctxwindow.uk"))
        page = await context.new_page()

        await page.goto(f"{server_url}/auth/login?demo=1", wait_until="networkidle")
        await page.evaluate("document.fonts.ready")

        # The dashboard defaults to a rolling 7 day range, but
        # seed_demo_db.py's timestamps are a fixed epoch (2026-08-01) so
        # they drift out of that window as real time passes. Selecting
        # "All time" (a real, existing filter control, not a hack) keeps
        # this reproducible regardless of which day it's actually run.
        await page.evaluate("window.setDashboardRange && window.setDashboardRange('all')")

        # rehydrateFromStorage() takes the injected token straight to the
        # dashboard; wait for the first real session row rather than a
        # fixed sleep, so this isn't flaky under a slower CI runner.
        await page.wait_for_selector(".session-row", timeout=15000)

        first_row = page.locator(".session-row").first
        await _click_with_beats(page, first_row)
        # Measured from context creation (recordVideo's own t=0), not
        # from just before the click, since that's the offset ffmpeg
        # needs to trim the "Select a session" empty state out of frame 0.
        trim_start_seconds = time.monotonic() - recording_started_at

        # 0.3s to 3.5s: demo_reveal.js is already staging the bar/rows/
        # KPIs in on its own timers from the moment renderSessionDetail
        # lands; nothing for this script to drive here except waiting
        # for that window to play out plus the settle beat (3.5s-4.5s).
        await page.wait_for_timeout(4500)

        context_tab = page.locator('.tab[data-tab="context"]')
        await _click_with_beats(page, context_tab)

        # 4.5s to 6.5s: zoom to the block list, then expand the row that
        # makes the point. The zoom is a CSS transform on a wrapper, not
        # an ffmpeg crop, so it stays in this one recording pass.
        await page.evaluate(
            """() => {
                const panel = document.querySelector('.tab-content.active[data-content="context"]')?.closest('.panel');
                if (!panel) return;
                panel.style.transition = 'transform 600ms ease-in-out';
                panel.style.transformOrigin = 'center 60%';
                panel.style.transform = 'scale(1.35)';
            }"""
        )
        await page.wait_for_timeout(700)

        target_row = page.locator(".block-row", has_text=expand_label).first
        await _click_with_beats(page, target_row)

        # 6.5s to 8.0s: hold on the expanded state, the still that shows
        # in the feed before playback starts.
        await page.wait_for_timeout(1300)

        await context.close()
        await browser.close()

    recorded = list(video_dir.glob("*.webm"))
    if not recorded:
        raise RuntimeError(f"Playwright didn't write a video into {video_dir}")
    return recorded[0], trim_start_seconds


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-url", default=DEFAULT_SERVER_URL)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--headed", action="store_true", help="Run with a visible browser window, for debugging.")
    parser.add_argument(
        "--variant",
        choices=sorted(EXPAND_VARIANTS),
        default="tool_result",
        help="Which context block to expand in the 4.5s-6.5s reveal step (candidate takes, see docs/internal/demo-takes.md).",
    )
    args = parser.parse_args()

    video_path, trim_start_seconds = asyncio.run(
        capture(args.server_url, args.out_dir, headless=not args.headed, expand_label=EXPAND_VARIANTS[args.variant])
    )
    print(f"Wrote raw capture to {video_path}")
    # Consumed by the Makefile's ffmpeg pass (-ss trim), printed as its
    # own line rather than folded into the sentence above so it's
    # trivially machine-parseable without a regex.
    print(f"TRIM_START_SECONDS={trim_start_seconds:.3f}")


if __name__ == "__main__":
    main()
