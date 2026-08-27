/**
 * Minimal reverse proxy: forwards every request to env.ORIGIN (the real
 * Cloud Run service URL) unchanged, and streams the response straight
 * back (response.body is already a ReadableStream, so this works for
 * SSE endpoints too, not just plain JSON/HTML). Gives this service a
 * short *.workers.dev URL without moving it off Cloud Run.
 *
 * It also stamps a Content-Security-Policy (and a few companion
 * security headers) onto every response on the way out. This is the
 * natural home for it: the worker already sits in front of every
 * request and rebuilds the response, so no new component is needed. The
 * origin app itself sets no CSP.
 *
 * See mcp-context-inspector's docs/DEPLOYMENT.md for the deploy command.
 *
 * Identical (proxy logic, not config) to sre-investigation-agent's own
 * cloudflare-proxy/worker.js, which fronts the chat UI's Cloud Run
 * service the same way. Not shared across repos on purpose (each is
 * independently deployable), but fix bugs in both copies.
 */

// The served HTML pages (mcp_server/routes/auth.py, routes/oauth.py) use
// inline <style> and inline <script> blocks *and* pervasive inline
// onclick= handlers, so script-src/style-src need 'unsafe-inline'.
// Nonces were considered and rejected: they don't cover event-handler
// attributes (only 'unsafe-hashes' + a hash per handler, or
// 'unsafe-inline', do), and there are dozens of onclick= handlers
// across the templates. What the policy still buys us: default-src
// locked to 'self', a tight allowlist for every fetchable resource
// type, and framing/base-uri/object-src shut off entirely.
//
//   fonts.googleapis.com  - the Google Fonts stylesheet (<link>)
//   fonts.gstatic.com     - the font files that stylesheet pulls
//   accounts.google.com   - Google Sign-In: loads /gsi/client (script),
//                           calls back (connect), and renders the
//                           sign-in button in an iframe (frame)
//
// All the page's own fetch() calls are same-origin, so connect-src
// needs only 'self' beyond accounts.google.com.
const CSP = [
  "default-src 'self'",
  "script-src 'self' 'unsafe-inline' https://accounts.google.com",
  "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
  "font-src 'self' https://fonts.gstatic.com",
  "img-src 'self' data:",
  "connect-src 'self' https://accounts.google.com",
  "frame-src 'self' https://accounts.google.com",
  "frame-ancestors 'none'",
  "base-uri 'none'",
  "object-src 'none'",
  "form-action 'self'",
].join("; ");

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const target = new URL(url.pathname + url.search, env.ORIGIN);

    const headers = new Headers(request.headers);
    // Let fetch() derive Host from the target URL. Forwarding the
    // original incoming Host (the workers.dev hostname) would send the
    // wrong SNI/Host to Cloud Run's origin.
    headers.delete("host");

    const init = {
      method: request.method,
      headers,
      body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
      redirect: "manual",
    };

    const response = await fetch(target.toString(), init);

    const outHeaders = new Headers(response.headers);
    // `new Headers(source)` can fold multiple Set-Cookie headers into one
    // comma-joined value, which corrupts every cookie after the first
    // (and would silently break the browser session). Re-emit each
    // Set-Cookie as its own header from the runtime's structured
    // accessor. No-op when the origin sends none.
    if (typeof response.headers.getSetCookie === "function") {
      const cookies = response.headers.getSetCookie();
      if (cookies.length) {
        outHeaders.delete("set-cookie");
        for (const cookie of cookies) {
          outHeaders.append("set-cookie", cookie);
        }
      }
    }
    outHeaders.set("content-security-policy", CSP);
    // Companion headers the CSP doesn't cover. frame-ancestors above
    // already blocks framing on modern browsers; X-Frame-Options is the
    // belt-and-braces version for older ones.
    outHeaders.set("x-content-type-options", "nosniff");
    outHeaders.set("x-frame-options", "DENY");
    outHeaders.set("referrer-policy", "strict-origin-when-cross-origin");

    return new Response(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers: outHeaders,
    });
  },
};
