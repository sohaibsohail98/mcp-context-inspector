"""Records the Context Window Explorer demo used for docs/demo.mp4 and
docs/demo.gif. Playwright drives a real Chromium against a real, locally
running instance of this server, in demo mode, against the deterministic
seeded database (see seed_demo_db.py), so the recording is reproducible:
same DOM, same timings, every run.

Two acts, recorded as one continuous video from one browser context so
the tab switch itself is a real Chromium tab change, not a cut:

1. A terminal mockup (demo_static/demo_terminal.html) types out the
   sample prompt, establishing "a developer just ran this in Claude
   Code". A real terminal emulator was ruled out as too fragile to
   automate deterministically for a marketing clip; see that file's own
   docstring comment for the full reasoning.
2. A real Chromium tab switch onto the actual dashboard, landing on the
   same seeded session's Context Window Explorer, answering "here is
   everything that went into that prompt's context".

Deliberately does not sign in via Google for the dashboard tab. auth.py's
rehydrateFromStorage() already trusts a token found in localStorage on
first load, so an addInitScript injecting CTXWINDOW's demo token there
before the first navigation reaches the dashboard exactly the way a
returning real user's browser would, without a real (and
non-deterministic) OAuth round trip.

No cursor is injected or driven anywhere in this recording: the tab
switch is a real Chromium tab activation (Playwright's Page.bring_to_
front, the same primitive a real Cmd+Tab-style switch triggers), and the
dashboard reveal itself (mcp_server/demo_static/demo_reveal.js) drives
every state change (bar fill, block expand) as a scripted DOM change, so
there is nothing for a pointer to point at anywhere in the clip.

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
from urllib.parse import quote

from playwright.async_api import async_playwright

from mcp_server.middleware import DEMO_TOKEN

REPO_ROOT = Path(__file__).parent.parent
DEFAULT_SERVER_URL = os.environ.get("DEMO_SERVER_URL", "http://127.0.0.1:8787")
DEFAULT_OUT_DIR = REPO_ROOT / "docs" / "_raw"

VIEWPORT = {"width": 1100, "height": 720}

# The seeded session's own prompt (see seed_demo_db.py's demo-session-12
# fixture) is the default: reusing the exact prompt the dashboard half
# then shows context for is what makes the tab switch land as "this is
# the same thing", rather than two unrelated demos glued together.
DEFAULT_PROMPT = "do a full multi-signal investigation of the checkout-api incident"

# Each reveal mode (see demo_reveal.js's REVEAL_MODES) needs its own hold
# time after the context tab is opened, since they drive different
# amounts of scripted activity (a single expand vs a three step tour).
# Measured against the actual REVEAL_MODES timers (fill window plus
# every scripted step's delay/hold) rather than guessed, with a few
# hundred ms of margin so the recording doesn't cut off mid-transition;
# see docs/internal/demo-takes-v2.md for the worked-out arithmetic.
#
# guided_tour is the only mode with two entries: after the user picked
# it as the sole shipped concept (see docs/internal/demo-takes.md), it
# grew a "cut" of its own (full for the LinkedIn video, short for the
# README gif) rather than being cloned into a second script, since the
# two cuts are the same choreography played for different beat counts,
# not two different concepts. The other three modes are unused since
# that decision but kept as single-entry so demo-candidates still works.
REVEAL_MODE_HOLD_MS = {
    # 4.2s fill + 200ms margin + 400ms settle + three 2.6/2.6/2.2s beats
    # (user prompt, tool call, reasoning), each long enough to actually
    # read, plus 600ms margin so the recording doesn't cut off mid-hold.
    ("guided_tour", "full"): 12800,
    # 2.8s fill + 200ms margin + 300ms settle + two 1.1s beats (user
    # prompt, tool result), no reasoning beat, plus 400ms margin: a
    # README gif is glanced at, not read attentively, so each beat gets
    # proportionally less time and there is one fewer of them.
    ("guided_tour", "short"): 5900,
    ("cost_reveal", "full"): 8600,  # kpi count-up leads, then fill, then tool specs callout
    ("surprise", "full"): 7750,  # fill, longer settle, then the single callout
    ("multi_turn", "full"): 7200,  # fill, then two grouped turn opens (setup, then turn 1)
}

# Typing speed for the terminal mockup, tuned per candidate take: a
# faster type reads punchier for a short LinkedIn/README clip, a slower
# one gives the prompt itself more time to register. Multiplied against
# demo_terminal.html's own per-character delay via the ?speed= param.
TERMINAL_MS_PER_CHAR = {
    ("guided_tour", "full"): 24,
    (
        "guided_tour",
        "short",
    ): 16,  # the short cut is glanced at, not read closely, so the opening typing beat can move faster
    ("cost_reveal", "full"): 20,  # quickest typing: this take is the most numbers-first, wants to get to the KPIs fast
    ("surprise", "full"): 28,
    ("multi_turn", "full"): 24,
}


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


async def capture(server_url, out_dir, headless=True, reveal_mode="guided_tour", cut="full", prompt=DEFAULT_PROMPT):
    """Returns (video_path, trim_start_seconds). Records one continuous
    video across both the terminal tab and the dashboard tab: Playwright
    writes one video per page in a context, so the terminal page's video
    file is the one this function returns, and the dashboard tab is
    recorded into a second file that gets stitched on afterwards by the
    Makefile's ffmpeg pass (concat, not crop or transform, since these
    are genuinely two different pages now rather than one page's
    internal state change)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    video_dir = out_dir / "video"
    if video_dir.exists():
        shutil.rmtree(video_dir)
    video_dir.mkdir(parents=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=2,
            record_video_dir=str(video_dir),
            record_video_size=VIEWPORT,
        )
        await context.add_init_script(_session_injection_script(DEMO_TOKEN, "demo@ctxwindow.uk"))

        # Act 1: the terminal mockup, opened first so it is frame 0 (no
        # session picker, no loading state, matching the original brief's
        # "the first frame must already be interesting" requirement, now
        # satisfied by the terminal chrome rather than the dashboard).
        terminal_page = await context.new_page()
        ms_per_char = TERMINAL_MS_PER_CHAR[(reveal_mode, cut)]
        terminal_url = f"{server_url}/demo-static/demo_terminal.html?prompt={quote(prompt)}&speed={ms_per_char}"
        await terminal_page.goto(terminal_url, wait_until="networkidle")
        await terminal_page.evaluate("document.fonts.ready")
        # Waits on the page's own typing promise rather than a fixed
        # sleep, so the hold time here always matches however long this
        # prompt actually takes to type at this speed.
        await terminal_page.evaluate("window.__demoTerminalReady")
        await terminal_page.wait_for_timeout(420)  # beat to read the "investigating" line before switching away

        # Act 2: the real tab switch. bring_to_front is Chromium's actual
        # tab activation, the same call a person's Cmd+Tab/Ctrl+Tab
        # triggers, recorded as its own video by Playwright rather than
        # simulated with a CSS transition on one page.
        dashboard_page = await context.new_page()
        recording_started_at = time.monotonic()
        await dashboard_page.goto(
            f"{server_url}/auth/login?demo=1&reveal={reveal_mode}&cut={cut}", wait_until="networkidle"
        )
        await dashboard_page.evaluate("document.fonts.ready")
        # demo_transition.js has already painted a full-screen overlay in
        # the shared background colour by this point (it runs inline,
        # synchronously, before the dashboard's own session rows have
        # loaded); bring_to_front lands on that held solid frame rather
        # than on a half-populated dashboard, so the switch itself reads
        # as clean regardless of how long the fetch below takes.
        await dashboard_page.bring_to_front()

        # Playwright's recordVideo keeps writing a page's video for as
        # long as that page stays open, not just while it's on screen:
        # left open, the terminal tab's clip would run for the entire
        # rest of the script (it did, in an earlier version of this
        # script, producing a ~14s terminal clip for what should have
        # been a ~3s beat). Closing it the moment the switch has
        # happened caps its recorded length to the actual terminal act.
        await terminal_page.close()

        # The dashboard defaults to a rolling 7 day range, but
        # seed_demo_db.py's timestamps are a fixed epoch (2026-08-01) so
        # they drift out of that window as real time passes. Selecting
        # "All time" (a real, existing filter control, not a hack) keeps
        # this reproducible regardless of which day it's actually run.
        await dashboard_page.evaluate("window.setDashboardRange && window.setDashboardRange('all')")

        # rehydrateFromStorage() takes the injected token straight to the
        # dashboard; wait for the first real session row rather than a
        # fixed sleep, so this isn't flaky under a slower CI runner.
        await dashboard_page.wait_for_selector(".session-row", timeout=15000)

        # Now that there is a real, populated dashboard behind it, fade
        # the transition overlay out: this is the cross-fade half of the
        # tab switch (see demo_transition.js), the terminal act's closing
        # hold is the other half, together they replace what used to be
        # a hard cut with no visual transition at all.
        await dashboard_page.evaluate("window.__demoRevealTransition && window.__demoRevealTransition()")

        first_row = dashboard_page.locator(".session-row").first
        await first_row.click()
        # Measured from this tab's own creation, not the terminal tab's,
        # since the Makefile trims the dashboard-half video independently
        # of the terminal-half video before concatenating them.
        trim_start_seconds = time.monotonic() - recording_started_at

        context_tab = dashboard_page.locator('.tab[data-tab="context"]')
        await context_tab.click()

        # The dashboard now groups blocks into collapsible turn sections
        # (id="ctx-block-list"). Bring the first real turn near the top of
        # the panel and drop the legend/filter-actions row out of frame
        # so the zoomed recording lands on the blocks themselves, not on
        # "Context blocks" / the ALL / NONE controls.
        await dashboard_page.evaluate(
            """() => {
                const list = document.getElementById('ctx-block-list');
                const groups = [...(list?.querySelectorAll('.turn-group') || [])];
                // Keep the pre-conversation group and turn 0 expanded (the
                // guided tour opens injected / user prompt / first tool
                // result, all in turn 0); collapse every later turn so the
                // whole tour sits in one screenful and the reveal never
                // scrolls away from the row it is pointing at.
                groups.forEach((g, i) => g.classList.toggle('collapsed', i >= 2));
                // Drop the legend/filter row out of frame for the zoomed shot.
                const legend = document.querySelector('.tab-content.active[data-content="context"] .ctx-legend');
                if (legend) legend.style.display = 'none';
                // Pin turn 0's heading just under the panel top.
                (groups[1] || list)?.scrollIntoView({ block: 'start' });
            }"""
        )
        await dashboard_page.wait_for_timeout(200)

        # Zoom to the block list so the breakdown is the visual focus for
        # the remainder of the recording; a CSS transform on a wrapper,
        # not an ffmpeg crop, so it stays in this one recording pass.
        await dashboard_page.evaluate(
            """() => {
                const panel = document.querySelector('.tab-content.active[data-content="context"]')?.closest('.panel');
                if (!panel) return;
                // ease-out (decelerate into rest), not ease-in-out: an
                // ease-in-out zoom has a slow-then-fast-then-slow feel
                // that reads as mechanical, whereas real UI motion (a
                // panel settling into place) only decelerates, it does
                // not also visibly accelerate out of rest first.
                panel.style.transition = 'transform 600ms cubic-bezier(0.16, 1, 0.3, 1)';
                panel.style.transformOrigin = 'center 40%';
                panel.style.transform = 'scale(1.2)';
            }"""
        )

        # demo_reveal.js drives every state change from here (bar fill,
        # KPI count-up, block opens) on its own timers keyed off the
        # reveal mode in the URL; this script just holds for long enough
        # to let that mode finish and settle on its final frame.
        await dashboard_page.wait_for_timeout(REVEAL_MODE_HOLD_MS[(reveal_mode, cut)])

        await context.close()
        await browser.close()

    # Playwright names recorded files by internal page id, not creation
    # order, so the two files are told apart by size (the dashboard
    # recording is always the longer one) rather than assumed sorted.
    recorded = sorted(video_dir.glob("*.webm"), key=lambda f: f.stat().st_size)
    if len(recorded) != 2:
        raise RuntimeError(
            f"expected exactly two recorded videos (terminal, dashboard) in {video_dir}, found {len(recorded)}"
        )
    terminal_video, dashboard_video = recorded[0], recorded[1]
    return terminal_video, dashboard_video, trim_start_seconds


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-url", default=DEFAULT_SERVER_URL)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--headed", action="store_true", help="Run with a visible browser window, for debugging.")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="Sample prompt typed in the terminal mockup.")
    parser.add_argument(
        "--reveal-mode",
        choices=sorted({mode for mode, _cut in REVEAL_MODE_HOLD_MS}),
        default="guided_tour",
        help="Which demo_reveal.js choreography to play (candidate takes, see docs/internal/demo-takes-v2.md).",
    )
    parser.add_argument(
        "--cut",
        choices=sorted({cut for _mode, cut in REVEAL_MODE_HOLD_MS}),
        default="full",
        help="'full' for the longer LinkedIn video, 'short' for the tighter README gif (guided_tour only, see docs/internal/demo-takes.md).",
    )
    args = parser.parse_args()

    if (args.reveal_mode, args.cut) not in REVEAL_MODE_HOLD_MS:
        raise SystemExit(f"no hold time configured for reveal mode {args.reveal_mode!r} with cut {args.cut!r}")

    terminal_video, dashboard_video, trim_start_seconds = asyncio.run(
        capture(
            args.server_url,
            args.out_dir,
            headless=not args.headed,
            reveal_mode=args.reveal_mode,
            cut=args.cut,
            prompt=args.prompt,
        )
    )
    print(f"Wrote terminal capture to {terminal_video}")
    print(f"Wrote dashboard capture to {dashboard_video}")
    # Consumed by the Makefile's ffmpeg pass (-ss trim on the dashboard
    # half only), printed as its own line rather than folded into the
    # sentence above so it's trivially machine-parseable without a regex.
    print(f"TRIM_START_SECONDS={trim_start_seconds:.3f}")


if __name__ == "__main__":
    main()
