// Staged reveal for the demo recording (scripts/demo_capture.py). Only
// loaded by /auth/login when CTXWINDOW_DEMO_MODE=1 (see routes/auth.py),
// and even then it no-ops unless ?demo=1 is on the URL, so this file has
// zero effect on the real dashboard the moment either gate is off.
//
// Deliberately a separate file that watches and replays the dashboard's
// own real render rather than a fork of renderContextTab/
// renderContextBlockRow: the production render path in auth.py stays
// completely unedited, so there is no risk of the demo path drifting
// from, or accidentally changing, what a real signed-in user sees.
//
// Reveal is one of several named modes (?reveal=guided_tour etc, see
// REVEAL_MODES below), not a single fixed choreography: the shipped
// "tool_result" take rushed the bar fill then span one block, and the
// point of this rewrite is a few structurally different ways of showing
// the same underlying render, chosen by scripts/demo_capture.py per take
// rather than hardcoded here.
(function () {
  "use strict";

  var params = new URLSearchParams(window.location.search);
  if (params.get("demo") !== "1") return;

  var reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var STAGGER_MS = 180;
  var mode = params.get("reveal") || "guided_tour";
  // guided_tour is the only mode that ships two cuts of itself (the
  // longer LinkedIn video and the tighter README gif): a query param
  // rather than a second mode name, since it is the same choreography
  // played for a different beat count/read time, not a different concept.
  var cut = params.get("cut") || "full";

  // A gentle deceleration curve for anything meant to read as settling
  // into place (bar fill, block rows appearing, the glow pulse): plain
  // "ease-out" already decelerates, but this cubic-bezier decelerates
  // more of the way through the motion, which is closer to how real UI
  // transitions (a panel or bar coming to rest) actually look rather
  // than constant-velocity motion with a flourish at the very end.
  var NATURAL_EASE = "cubic-bezier(0.16, 1, 0.3, 1)";

  function parseCount(text) {
    // KPI text is already formatted ("48.2k", "$0.94", "84%"), so the
    // count-up animates the underlying number and re-applies the
    // original string's prefix/suffix rather than reparsing units.
    var match = /^([^0-9.-]*)([0-9.,]+)(.*)$/.exec(text || "");
    if (!match) return null;
    return { prefix: match[1], value: parseFloat(match[2].replace(/,/g, "")), suffix: match[3] };
  }

  function countUp(el, durationMs, onDone) {
    var parsed = parseCount(el.textContent);
    if (!parsed || reduceMotion) { if (onDone) onDone(); return; }
    var target = parsed.value;
    var start = null;
    function frame(ts) {
      if (start === null) start = ts;
      var t = Math.min(1, (ts - start) / durationMs);
      var eased = 1 - Math.pow(1 - t, 3);
      var shown = target * eased;
      var decimals = parsed.value % 1 !== 0 ? (parsed.suffix.indexOf("%") === 0 ? 0 : 2) : 0;
      el.textContent = parsed.prefix + shown.toFixed(decimals) + parsed.suffix;
      if (t < 1) requestAnimationFrame(frame);
      else { el.textContent = parsed.prefix + parsed.value + parsed.suffix; if (onDone) onDone(); }
    }
    requestAnimationFrame(frame);
  }

  // A CSS class rather than inline animation properties: several reveal
  // modes need the same "this block matters right now" cue (guided tour
  // steps, the surprise callout), so one rule shared across modes keeps
  // the visual language consistent between takes instead of each mode
  // inventing its own glow.
  function injectGlowStyle() {
    var style = document.createElement("style");
    style.textContent =
      "@keyframes __demo_glow_pulse {" +
      "  0% { box-shadow: 0 0 0 0 rgba(108,191,164,0.55); background: var(--bg-raised-2); }" +
      "  40% { box-shadow: 0 0 0 8px rgba(108,191,164,0); background: rgba(108,191,164,0.14); }" +
      "  100% { box-shadow: 0 0 0 0 rgba(108,191,164,0); background: var(--bg-raised-2); }" +
      "}" +
      ".__demo_glow { animation: __demo_glow_pulse 900ms " + NATURAL_EASE + "; }" +
      ".__demo_callout {" +
      "  position: absolute; transform: translateY(-100%);" +
      "  background: #6cbfa4; color: #17150f; font: 650 11px/1.3 Archivo, sans-serif;" +
      "  padding: 0.3rem 0.55rem; border-radius: 6px; white-space: nowrap;" +
      "  opacity: 0; transition: opacity 220ms " + NATURAL_EASE + ", transform 220ms " + NATURAL_EASE + ";" +
      "  z-index: 5; pointer-events: none;" +
      "}" +
      ".__demo_callout.show { opacity: 1; transform: translateY(-115%); }";
    document.head.appendChild(style);
  }

  // Waits for the real dashboard render (renderContextTab, populated
  // with the seeded session's actual blocks) to land, then stages it.
  // Polling rather than hooking refreshDashboard directly: the render
  // functions are closures inside auth.py's inline script with nothing
  // exported for another file to call into, and adding an export there
  // is exactly the "thread conditionals through the existing render
  // path" this file exists to avoid.
  function waitFor(selector, cb, attempts) {
    var el = document.querySelector(selector);
    if (el) return cb(el);
    if (attempts <= 0) return;
    setTimeout(function () { waitFor(selector, cb, attempts - 1); }, 100);
  }

  // Opens a row the same way a real click would (toggleBlockDetail is
  // untouched production code), but called directly rather than
  // dispatched as a click: there is no cursor in this recording any
  // more, so a scripted state change is the honest way to represent
  // "this row is now open" instead of faking a pointer event that
  // implies a click nobody will see happen.
  function openRow(row, withGlow) {
    if (!row) return;
    var idx = row.id.replace("block-row-", "");
    if (window.toggleBlockDetail) window.toggleBlockDetail(idx);
    if (withGlow) {
      row.classList.add("__demo_glow");
      setTimeout(function () { row.classList.remove("__demo_glow"); }, 900);
    }
    row.scrollIntoView({ block: "nearest" });
  }

  function findRow(rows, labelSubstring) {
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].textContent.indexOf(labelSubstring) !== -1) return rows[i];
    }
    return null;
  }

  function showCallout(row, text, holdMs) {
    if (!row) return;
    var callout = document.createElement("div");
    callout.className = "__demo_callout";
    callout.textContent = text;
    row.style.position = "relative";
    row.appendChild(callout);
    requestAnimationFrame(function () { callout.classList.add("show"); });
    setTimeout(function () {
      callout.classList.remove("show");
      setTimeout(function () { callout.remove(); }, 250);
    }, holdMs);
  }

  // Shared bar/row fill-in, used by every mode: this is "the whole idea
  // of the tool expressed visually" per the brief, so it is always run
  // in full rather than being an optional step some modes skip.
  //
  // The row stagger is capped to fit inside fillWindowMs rather than
  // using a fixed 180ms/row unconditionally: demo-session-12 (the
  // seeded 15-turn investigation) renders around 45 block rows, and
  // 180ms each would push the last row in nearly 8s after the bar
  // finishes, which then delayed every later scripted step (block
  // opens, KPI count-up) by the same amount, since onDone used to wait
  // on whichever of the two (bar or rows) took longer. Capping the
  // stagger keeps "done" meaning what it says regardless of how many
  // rows a given session happens to have.
  function fillBreakdown(segments, rows, targetWidths, fillWindowMs, onDone) {
    var perSegmentMs = fillWindowMs / segments.length;
    segments.forEach(function (seg, i) {
      setTimeout(function () { seg.style.width = targetWidths[i]; }, i * perSegmentMs);
    });
    var rowStagger = rows.length > 1 ? Math.min(STAGGER_MS, fillWindowMs / rows.length) : 0;
    rows.forEach(function (row, i) {
      setTimeout(function () {
        row.style.opacity = "1";
        row.style.transform = "none";
      }, i * rowStagger);
    });
    setTimeout(onDone, fillWindowMs + 200);
  }

  function zeroOut(segments, rows, kpis) {
    var targetWidths = segments.map(function (seg) { return seg.style.width; });
    segments.forEach(function (seg) { seg.style.transition = "width 260ms " + NATURAL_EASE; seg.style.width = "0%"; });
    rows.forEach(function (row) {
      row.style.transition = "opacity 200ms " + NATURAL_EASE + ", transform 200ms " + NATURAL_EASE;
      row.style.opacity = "0";
      row.style.transform = "translateY(4px)";
      // Detail panels stay in the DOM (already .hidden by default), only
      // the row itself is staged in; toggleBlockDetail() is untouched.
    });
    var kpiOriginals = kpis.map(function (el) { return el.textContent; });
    kpis.forEach(function (el) { el.textContent = el.textContent.replace(/[0-9.]+/, "0"); });
    return { targetWidths: targetWidths, kpiOriginals: kpiOriginals };
  }

  function restoreAndCountKpis(kpis, kpiOriginals, durationMs) {
    kpis.forEach(function (el, i) {
      el.textContent = kpiOriginals[i];
      countUp(el, durationMs);
    });
  }

  // REVEAL_MODES: each mode gets the same raw material (bar segments,
  // block rows already rendered by the real dashboard, the three
  // .metric-tile KPIs) and decides its own pacing/ordering. Selected via
  // scripts/demo_capture.py's --reveal-mode, one mode per candidate take
  // (see docs/internal/demo-takes-v2.md).
  var REVEAL_MODES = {
    // Bar fill gets the majority of the runtime (per the brief), then
    // three genuinely different block kinds open in sequence so a
    // first-time viewer sees several examples of "context you can
    // inspect", not one cherry-picked block. This is the structural
    // opposite of every prior take, which spent most of its time on one
    // expand.
    // The winning concept (see docs/internal/demo-takes.md): plays two
    // different cuts of the same choreography rather than two separate
    // modes, since "full" and "short" differ only in fill length, beat
    // count and hold time, not in what actually happens on screen.
    //
    // The user's own feedback after reviewing all four candidates was
    // that this one needed "a few more seconds in between user prompt,
    // tool call and reasoning to show to the user so they can read it":
    // the full cut's holds (2600/2600/2200ms) are roughly 2.3x the
    // original single-take values (1100/1100/900ms) for exactly that
    // reason. scripts/demo_capture.py's REVEAL_MODE_HOLD_MS is derived
    // from the same arithmetic used here, so the two files must be
    // changed together if either side's timing moves again.
    guided_tour: function (ctx) {
      var state = zeroOut(ctx.segments, ctx.rows, ctx.kpis);
      var full = cut !== "short";
      var fillWindowMs = full ? 4200 : 2800; // short cut trims the fill too, not just the beats after it
      fillBreakdown(ctx.segments, ctx.rows, state.targetWidths, fillWindowMs, function () {
        restoreAndCountKpis(ctx.kpis, state.kpiOriginals, fillWindowMs);
        // Full: all three block kinds, generous read time on each. Short:
        // just the two most illustrative ones (user prompt, tool result),
        // dropping the reasoning beat rather than shortening all three
        // equally, since a README gif is glanced at rather than watched
        // attentively and benefits more from fewer beats than from the
        // same beats rushed.
        var tour = full
          ? [
              { label: "User prompt", hold: 2600 },
              { label: "Tool result", hold: 2600 },
              { label: "Reasoning", hold: 2200 },
            ]
          : [
              { label: "User prompt", hold: 1100 },
              { label: "Tool result", hold: 1100 },
            ];
        var t = full ? 400 : 300; // settle beat before the tour starts
        tour.forEach(function (step) {
          var row = findRow(ctx.rows, step.label);
          setTimeout(function () { openRow(row, true); }, t);
          t += step.hold;
          // Close it before moving on, except the last one, which stays
          // open for the final held frame.
          if (step !== tour[tour.length - 1]) {
            (function (r, closeAt) { setTimeout(function () { openRow(r, false); }, closeAt); })(row, t);
          }
        });
      });
    },

    // Leads with the KPI strip counting up (cost/tokens/cache first),
    // reversing the brief's original bar-first order, then the bar fills
    // as the visual "why", then the single most surprising block (tool
    // specs) opens with an explicit callout rather than a bare expand.
    // This is the only mode where the numbers, not the bar, are the
    // first thing on screen.
    cost_reveal: function (ctx) {
      var state = zeroOut(ctx.segments, ctx.rows, ctx.kpis);
      var kpiWindowMs = 1400;
      setTimeout(function () {
        restoreAndCountKpis(ctx.kpis, state.kpiOriginals, kpiWindowMs);
      }, 300);
      setTimeout(function () {
        var fillWindowMs = 3000;
        fillBreakdown(ctx.segments, ctx.rows, state.targetWidths, fillWindowMs, function () {
          var row = findRow(ctx.rows, "Tool specs");
          setTimeout(function () {
            openRow(row, true);
            // Held well past this mode's own runtime (see
            // scripts/demo_capture.py's REVEAL_MODE_HOLD_MS) so the
            // callout is still on screen, not mid fade-out, on the
            // final frame the recording actually stops on.
            showCallout(row, "5 tool schemas, sent every turn", 6000);
          }, 500);
        });
      }, 300 + kpiWindowMs + 300);
    },

    // The surprise, given more room than the earlier "tools" take: bar
    // fill, a longer beat to let the completed bar register on its own,
    // then the reveal with an explicit annotation instead of a bare
    // expand so the point (tool specs cost real tokens) reads even with
    // no narration.
    surprise: function (ctx) {
      var state = zeroOut(ctx.segments, ctx.rows, ctx.kpis);
      var fillWindowMs = 3400;
      fillBreakdown(ctx.segments, ctx.rows, state.targetWidths, fillWindowMs, function () {
        restoreAndCountKpis(ctx.kpis, state.kpiOriginals, fillWindowMs);
        setTimeout(function () {
          var row = findRow(ctx.rows, "Tool specs");
          openRow(row, true);
          // Held well past this mode's own runtime, same reasoning as
          // cost_reveal's callout above.
          showCallout(row, "Tool specs: bigger than the user prompt", 6000);
        }, 900); // longer settle than prior takes, so the full bar registers before the twist
      });
    },

    // Groups the existing flat block list by its real turn_n boundaries
    // (already present in the rendered rows, no schema change) and
    // steps through them turn by turn, showing context accumulating
    // rather than a single static snapshot. Distinct from guided_tour:
    // that mode picks by block kind, this one picks by turn.
    multi_turn: function (ctx) {
      var state = zeroOut(ctx.segments, ctx.rows, ctx.kpis);
      var fillWindowMs = 3600;
      fillBreakdown(ctx.segments, ctx.rows, state.targetWidths, fillWindowMs, function () {
        restoreAndCountKpis(ctx.kpis, state.kpiOriginals, fillWindowMs);
        // The setup row opens and STAYS open (unlike guided_tour, which
        // closes each step before the next) while turn 1's row also
        // opens alongside it: both visible together on the final frame
        // is what actually shows "accumulation", one block replacing
        // another in the same slot would just look like guided_tour
        // with different labels.
        var setupRow = findRow(ctx.rows, "User prompt");
        var turn1Row = findRow(ctx.rows, "Tool result: list_services");
        setTimeout(function () { openRow(setupRow, true); }, 400);
        setTimeout(function () { openRow(turn1Row, true); }, 1900);
      });
    },
  };

  function stageReveal() {
    var bar = document.querySelector(".ctx-bar");
    var list = document.querySelector(".block-list");
    if (!bar || !list) return;

    var segments = Array.prototype.slice.call(bar.children);
    var rows = Array.prototype.slice.call(list.children).filter(function (el) {
      return el.classList.contains("block-row");
    });
    if (!segments.length || !rows.length) return;

    // The brief names #kpi-tokens/#kpi-cache/#kpi-cost/#kpi-context, but
    // those ids only exist on the pre-sign-in landing hero card (see
    // auth_login's "intro" template), not on the authenticated
    // dashboard's own Overview tab, which has no ids at all on its
    // .metric-tile values. Counting up the tiles that are actually on
    // screen next to the block list (Tokens, Cache hit, Cost) is the
    // faithful reading of "count the KPI values up" for this view;
    // Tool calls stays as-is since it's a plain integer, not worth
    // animating.
    var kpis = Array.prototype.slice.call(document.querySelectorAll(".metric-tile .m-value")).slice(0, 3);

    if (reduceMotion) return; // already at final state, nothing to stage

    injectGlowStyle();
    var run = REVEAL_MODES[mode] || REVEAL_MODES.guided_tour;
    run({ segments: segments, rows: rows, kpis: kpis });
  }

  // Every tab's content is rendered up front into the DOM at session
  // select time (see renderSessionDetail in auth.py), including the
  // Context Explorer tab while the Overview tab is still the one
  // visible, so waiting only for ".block-row" to exist would run (and
  // finish) the whole staged reveal on a tab nobody can see yet. Waiting
  // for the context tab-content to actually carry ".active" as well
  // means the reveal only plays once a viewer (or the capture script's
  // tab switch) can actually see it happen.
  function contextTabIsActive() {
    var content = document.querySelector('.tab-content[data-content="context"]');
    return !!(content && content.classList.contains("active"));
  }

  waitFor(".block-list .block-row", function () {
    function waitForActiveTab(attempts) {
      if (contextTabIsActive()) {
        // One more frame so layout has settled before capturing "real"
        // widths, otherwise a still-collapsing flex container can report 0.
        requestAnimationFrame(stageReveal);
        return;
      }
      if (attempts <= 0) return;
      setTimeout(function () { waitForActiveTab(attempts - 1); }, 100);
    }
    waitForActiveTab(80); // up to 8s, generous since this is capture tooling, not a real page load budget
  }, 50);
})();
