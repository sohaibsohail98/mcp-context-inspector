"""Best-effort human label for a per-device / per-session token, derived
from the request User-Agent at mint time.

Deliberately tiny and dependency-free: the goal is a recognisable string
in the dashboard's "Devices" list ("Chrome on macOS", "Claude Code CLI",
"claude.ai Connector"), not real UA parsing. Anything unrecognised falls
back to "Unknown device", which is also what a token minted before this
metadata existed shows as (its row simply doesn't carry a label; see
list_tokens() in each backend).

Shared by all three auth-store backends and by routes/auth.py so the
label a token is stored with and the label shown in the UI can never
drift apart.
"""

# Order matters: the first match wins, so the more specific agents
# (Claude Code, the claude.ai connector) are checked before the generic
# browser families.
_AGENT_RULES = [
    ("claude code", "Claude Code CLI"),
    ("claude-code", "Claude Code CLI"),
    ("claude-user", "claude.ai Connector"),
    ("claude.ai", "claude.ai Connector"),
    ("anthropic", "claude.ai Connector"),
    ("node-fetch", "MCP client"),
    ("python-requests", "MCP client"),
    ("python-httpx", "MCP client"),
    ("httpx", "MCP client"),
    ("curl", "Command line (curl)"),
    ("okhttp", "MCP client"),
]

_BROWSER_RULES = [
    ("edg/", "Edge"),
    ("edga/", "Edge"),
    ("edgios/", "Edge"),
    ("opr/", "Opera"),
    ("firefox/", "Firefox"),
    ("fxios/", "Firefox"),
    # Chrome must come before Safari: every Chrome UA also contains
    # "safari/". Safari is only the ones that have "safari/" but not
    # "chrome/" / "chromium/".
    ("chromium/", "Chromium"),
    ("chrome/", "Chrome"),
    ("crios/", "Chrome"),
    ("safari/", "Safari"),
]

_OS_RULES = [
    ("windows nt", "Windows"),
    ("mac os x", "macOS"),
    ("macintosh", "macOS"),
    ("cros", "ChromeOS"),
    ("android", "Android"),
    ("iphone", "iOS"),
    ("ipad", "iPadOS"),
    ("linux", "Linux"),
]

UNKNOWN_DEVICE = "Unknown device"


def label_for_user_agent(user_agent):
    """Map a raw User-Agent header value to a short display label.

    Returns "Unknown device" for an empty/missing/unrecognised UA, the
    same string list_tokens() synthesises for a pre-metadata token, so
    the two are indistinguishable in the UI (which is the intent: an old
    token isn't wrong, just unlabelled)."""
    ua = (user_agent or "").strip().lower()
    if not ua:
        return UNKNOWN_DEVICE

    for needle, label in _AGENT_RULES:
        if needle in ua:
            return label

    browser = next((label for needle, label in _BROWSER_RULES if needle in ua), None)
    os_name = next((label for needle, label in _OS_RULES if needle in ua), None)

    if browser and os_name:
        return f"{browser} on {os_name}"
    if browser:
        return browser
    if os_name:
        return f"Browser on {os_name}"
    return UNKNOWN_DEVICE
