/**
 * Minimal reverse proxy: forwards every request to env.ORIGIN (the real
 * Cloud Run service URL) unchanged, and streams the response straight
 * back (response.body is already a ReadableStream, so this works for
 * SSE endpoints too, not just plain JSON/HTML). Gives this service a
 * short *.workers.dev URL without moving it off Cloud Run.
 *
 * See mcp-context-inspector's docs/DEPLOYMENT.md for the deploy command.
 *
 * Identical (proxy logic, not config) to sre-investigation-agent's own
 * cloudflare-proxy/worker.js, which fronts the chat UI's Cloud Run
 * service the same way. Not shared across repos on purpose (each is
 * independently deployable), but fix bugs in both copies.
 */
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
    return new Response(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers: response.headers,
    });
  },
};
