# Claude Code brief: automated demo capture for CtxWindow

Run this as a single fresh session with `/clear` beforehand. Plan mode first, no edits until the plan is approved.

---

## Prompt

You are adding an automated demo capture pipeline to this repo. The output is two files, a square MP4 for LinkedIn and a GIF for the README, both generated from a single Playwright script so the demo is reproducible rather than hand-recorded. Work in plan mode first and show me the plan before touching anything.

### What the repo already gives you

Read these before planning, they matter:

- `scripts/seed_demo_db.py` produces `demo/metrics.db` with fixed IDs and a fixed base epoch of `1785542400.0`. It is deliberately deterministic, the docstring explains why. The demo must run against this database and nothing else, no live Bedrock calls, no wall-clock timestamps.
- `mcp_server/routes/auth.py` serves the desktop dashboard. The Context Window Explorer lives there: `.ctx-bar` is the stacked proportional bar, `.block-list` holds `.block-row` elements, each with `.block-dot`, `.block-label`, `.block-tok`, `.block-pct` and a `.block-chev` that rotates when the row gets the `expanded` class to reveal `.block-detail`. This is the demo surface. Do not use the mobile webapp in `webapp/`, it is a narrower session-history view and the stacked bar is the thing worth showing.
- Category colours are CSS custom properties on `:root` in the same file: `--cat-system`, `--cat-tools`, `--cat-user`, `--cat-reasoning`, `--cat-toolcall`, `--cat-toolresult`, `--cat-answer`. The palette is warm dark, background `#17150f`, accent `#6cbfa4`. Keep everything in that palette, no external branding.
- Fonts are Source Serif 4, Archivo and JetBrains Mono, loaded from Google Fonts. The script must wait on `document.fonts.ready` before the first frame or the opening frames will show fallback faces.

### Authentication

The dashboard sits behind Google sign-in. Do not script the sign-in flow, it is non-deterministic and will put a real account in the recording. Inject a demo session token into `localStorage` via `addInitScript` before the first navigation, and add a demo-only bypass on the server side gated behind an environment variable such as `CTXWINDOW_DEMO_MODE=1`. That variable must not be readable in any production path and must default off. Flag it to me if the cleanest bypass touches auth code, I want to see that diff specifically.

### The staged reveal

The dashboard currently renders the full block list in one paint, which is correct for real use and useless for a demo. Add a demo-only reveal mode, triggered by a `?demo=1` query parameter and only honoured when demo mode is enabled, that:

1. Starts with the `.ctx-bar` at zero width and the `.block-list` empty.
2. Animates each segment of the stacked bar in, in load order, system prompt then tool specs then user prompt then the per-turn blocks.
3. Appends the matching `.block-row` as each segment lands, with a short stagger, roughly 180ms.
4. Counts the KPI values up rather than snapping, using the existing `#kpi-tokens`, `#kpi-cache`, `#kpi-cost` and `#kpi-context` elements.

Respect `prefers-reduced-motion` by skipping straight to the final state. Put this in a separate file rather than threading conditionals through the existing render path, and make sure the production render is byte-identical when demo mode is off.

### The capture script

Create `scripts/demo_capture.py` or a Node equivalent, your call, but keep it consistent with how the rest of the repo is structured. Requirements:

- Playwright, Chromium, `recordVideo` with `deviceScaleFactor: 2` so text survives downscaling.
- Viewport `1100x720`. Small viewport rather than a large one shrunk afterwards, because downscaling is what destroys legibility.
- Inject a fake cursor. Playwright drives the DOM and produces no visible pointer, so add a small absolutely positioned element via `addInitScript` that tracks mouse coordinates. Style it to match the dashboard, do not use a stock arrow PNG.
- Every pointer move uses `page.mouse.move(x, y, { steps: 25 })` with a 300ms beat before the click and a 400ms beat after. Instantaneous actions read as a rendering glitch, the beats are what make it look captured rather than generated.
- Wait on network idle and `document.fonts.ready` before recording anything meaningful.

### The choreography

Eight seconds, no more. Script it exactly as follows, and tune the timings until it lands under eight:

1. **0.0s** Open on the dashboard with the session already selected and the bar at zero. No loading state, no empty state, no sign-in screen. The first frame must already be interesting.
2. **0.3s to 3.5s** The stacked bar fills left to right as blocks load, colour by colour, with rows appearing beneath in step. KPIs count up alongside. This is the whole idea of the tool expressed visually and it needs to be the majority of the runtime.
3. **3.5s to 4.5s** Beat. Everything settles. Let the viewer read the completed bar.
4. **4.5s to 6.5s** Zoom to the block list and expand one `.block-row` to reveal its `.block-detail`. Pick a block that makes the point, tool specs or an injected reminder, something a viewer would not have guessed was consuming tokens.
5. **6.5s to 8.0s** Hold on the expanded state. End on a readable frame, since this is the still that shows in the feed before playback starts.

For the zoom, apply a CSS transform on a wrapper with `transform-origin` set to the block list, easing over roughly 600ms. Do not use ffmpeg crop for this, an animated transform reads better and keeps the whole thing in one pass.

### Outputs

Two files, both written to `docs/` and both gitignored as build artefacts unless I say otherwise:

- `docs/demo.mp4`, square 1080x1080, letterboxed onto the dashboard background colour `#17150f` rather than black bars, since LinkedIn favours square in the feed.
- `docs/demo.gif`, 900px wide, 15fps, under 5MB. Use the two-pass palette method, `palettegen` with `stats_mode=diff` then `paletteuse` with `dither=bayer:bayer_scale=3`. A single pass will produce a dithered mess.

Expose both behind one command, `make demo` or an equivalent script entry, that reseeds the database, starts the server in demo mode, runs the capture, converts, and tears down. It must work from a clean checkout with no manual steps. Print the final file sizes and durations so I can see at a glance whether it is within spec.

### Constraints

- British spelling in all prose, comments and commit messages. No em-dashes or en-dashes anywhere, use commas or brackets.
- Comments explain why, not what. Do not restate what the line above does. If a decision needs three sentences of justification, that belongs in a docstring or in `docs/internal/`, not inline.
- Granular commits, one logical change each. Do not squash.
- Stop and ask before modifying anything in `mcp_server/routes/auth.py` beyond the demo gate, before adding any new runtime dependency, and before writing anything outside `scripts/`, `docs/` and the demo module.
- No new production dependencies. Playwright and ffmpeg are dev and CI only, keep them out of the main dependency list in `pyproject.toml`.

Start by reading the files listed above and showing me your plan.

---

## After it runs

Check the first frame and the last frame in isolation, because those two do most of the work. The first is what people see while scrolling and the last is the one they sit on. If either is ambiguous, adjust the choreography rather than the encoding.

Then regenerate once more before posting, to confirm the pipeline is genuinely reproducible and not accidentally dependent on something in your local state.
