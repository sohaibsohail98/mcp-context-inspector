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
(function () {
  "use strict";

  var params = new URLSearchParams(window.location.search);
  if (params.get("demo") !== "1") return;

  var reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var STAGGER_MS = 180;

  function parseCount(text) {
    // KPI text is already formatted ("48.2k", "$0.94", "84%"), so the
    // count-up animates the underlying number and re-applies the
    // original string's prefix/suffix rather than reparsing units.
    var match = /^([^0-9.-]*)([0-9.,]+)(.*)$/.exec(text || "");
    if (!match) return null;
    return { prefix: match[1], value: parseFloat(match[2].replace(/,/g, "")), suffix: match[3] };
  }

  function countUp(el, durationMs) {
    var parsed = parseCount(el.textContent);
    if (!parsed || reduceMotion) return;
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
      else el.textContent = parsed.prefix + parsed.value + parsed.suffix;
    }
    requestAnimationFrame(frame);
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

    // Capture real widths/visibility before zeroing, so the replay
    // below animates towards the values the server actually rendered
    // rather than guessed placeholders.
    var targetWidths = segments.map(function (seg) { return seg.style.width; });
    segments.forEach(function (seg) { seg.style.transition = "width 260ms ease-out"; seg.style.width = "0%"; });
    rows.forEach(function (row) {
      row.style.transition = "opacity 200ms ease-out, transform 200ms ease-out";
      row.style.opacity = "0";
      row.style.transform = "translateY(4px)";
      // Detail panels stay in the DOM (already .hidden by default), only
      // the row itself is staged in; toggleBlockDetail() is untouched.
    });

    var kpiOriginals = kpis.map(function (el) { return el.textContent; });
    kpis.forEach(function (el) { el.textContent = el.textContent.replace(/[0-9.]+/, "0"); });

    var revealWindowMs = 3200; // 0.3s to 3.5s in the choreography, minus the initial settle
    var perSegmentMs = revealWindowMs / segments.length;

    segments.forEach(function (seg, i) {
      setTimeout(function () { seg.style.width = targetWidths[i]; }, i * perSegmentMs);
    });
    rows.forEach(function (row, i) {
      setTimeout(function () {
        row.style.opacity = "1";
        row.style.transform = "none";
      }, i * STAGGER_MS);
    });
    kpis.forEach(function (el, i) {
      el.textContent = kpiOriginals[i]; // restore full string as the count-up start point
      countUp(el, revealWindowMs);
    });
  }

  waitFor(".block-list .block-row", function () {
    // One more frame so layout has settled before capturing "real"
    // widths, otherwise a still-collapsing flex container can report 0.
    requestAnimationFrame(stageReveal);
  }, 50);
})();
