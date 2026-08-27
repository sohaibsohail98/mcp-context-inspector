// Demo-only entry transition for the dashboard tab (scripts/demo_capture.py).
// Only loaded by /auth/login when CTXWINDOW_DEMO_MODE=1 and ?demo=1 (see
// routes/auth.py, same double gate as demo_reveal.js), so this has zero
// effect on a real user's dashboard.
//
// The tab switch itself (Playwright's Page.bring_to_front) is an instant
// activation with no visual transition of its own, which is what made the
// cut from the terminal mockup to the dashboard read as an abrupt jump
// rather than an intentional switch. Rather than try to animate the
// terminal page (which is about to be closed and has nothing to do with
// the dashboard's own render), this paints a full-screen overlay in the
// shared background colour on the dashboard page itself and fades it out
// the moment the page is ready to be shown, so bring_to_front lands on a
// held solid frame that then dissolves into the real UI: a cheap
// cross-fade that costs nothing on the terminal side and needs no
// coordination between the two pages.
(function () {
  "use strict";

  var params = new URLSearchParams(window.location.search);
  if (params.get("demo") !== "1") return;

  var reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduceMotion) return;

  var overlay = document.createElement("div");
  overlay.id = "__demo_transition_overlay";
  overlay.style.cssText =
    "position:fixed; inset:0; z-index:9999; background:#17150f;" +
    "opacity:1; transition:opacity 320ms ease-out; pointer-events:none;";
  // Inserted as early as possible (this script runs before the rest of
  // the page's own content settles) so there is no flash of the real UI
  // underneath before the overlay is in place.
  document.documentElement.appendChild(overlay);

  // Deliberately NOT tied to window.load: the dashboard shell paints
  // long before its session rows do (those arrive from an async fetch,
  // see refreshDashboard in auth.py), so fading on load would reveal an
  // empty "Sessions" panel for a beat. Exposed instead as a function
  // scripts/demo_capture.py calls explicitly, once it has confirmed via
  // its own wait_for_selector(".session-row") that there is something
  // real to fade in to.
  window.__demoRevealTransition = function () {
    // rAF, not an immediate style write: the opacity transition needs
    // the element to have already painted at opacity 1 on a previous
    // frame, otherwise the browser can coalesce both writes and skip
    // the fade entirely.
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        overlay.style.opacity = "0";
        setTimeout(function () { overlay.remove(); }, 360);
      });
    });
  };
})();
