# Lucid architecture diagram prompt (ctxwindow) — simple version

Paste this into Lucid's AI diagram generator for a clean, presentable board. This
replaces the earlier detailed version, which produced a dense engineering diagram
that's hard to read live. Keep it to one page, five boxes, and a short numbered flow.

---

Create a simple, clean architecture diagram with five boxes arranged left to right,
plus one numbered flow underneath. Dark background, one accent colour, generous
spacing, large readable labels. This is for a live presentation, not documentation,
so favour clarity over completeness.

## The five boxes (left to right, connected by a single arrow each)

1. **Claude Code / MCP Client** — "Your AI coding tool"
2. **Cloudflare** — "Edge + security (ctxwindow.uk)"
3. **Cloud Run** — "The MCP server (Python)"
4. **Firestore** — "Session + metrics storage"
5. **Google Sign-In** — "Identity" (draw this one above box 3, connecting down into it,
   rather than in the left-to-right line, since it's how a person authenticates rather
   than a step in the data flow)

Label each arrow with one short phrase, not a protocol name: "sends requests",
"proxies + protects", "reads/writes", "verifies who you are".

## Underneath: a simple 4-step numbered flow

1. You sign in with Google
2. Claude Code connects using a token
3. The server reads/writes your session data
4. You see it live on the dashboard

## Style

- Dark background (near-black), one warm accent colour for the boxes, white/light
  text, generous padding, rounded rectangles.
- No fine print, no protocol names in the boxes themselves, no IAM detail, no CI/CD.
- One page only. If Lucid AI offers to add more detail or more boxes, decline; the
  goal is something a non-technical person in the room can read from across a table
  in three seconds.
