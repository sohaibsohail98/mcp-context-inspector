"""Shared merge/backup logic for writing this account's MCP connection +
OTLP telemetry config into a Claude Code ~/.claude/settings.json — used by
both:

- The /setup/apply-local-config route, when the server itself is running
  on the caller's own machine (self-hosted, loopback).
- The downloadable script templated by /setup/local-script (see
  LOCAL_SCRIPT_TEMPLATE below), when the server is deployed elsewhere and
  can't reach the caller's filesystem directly — the script runs this same
  logic locally instead.
"""

import json
import time
from pathlib import Path

SETTINGS_PATH = Path.home() / ".claude" / "settings.json"


def build_settings_patch(base_url, bearer_token):
    """The mcpServers + env fragment to merge into settings.json, given the
    server's base URL and the caller's bearer token."""
    base = base_url.rstrip("/")
    return {
        "mcpServers": {
            "context-inspector": {
                "url": base + "/mcp",
                "headers": {"Authorization": "Bearer " + bearer_token},
            },
        },
        "env": {
            "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
            "OTEL_LOGS_EXPORTER": "otlp",
            "OTEL_METRICS_EXPORTER": "otlp",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
            "OTEL_EXPORTER_OTLP_ENDPOINT": base + "/otlp",
            "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer " + bearer_token,
            "OTEL_LOG_RAW_API_BODIES": "1",
            "CLAUDE_CODE_OTEL_CONTENT_MAX_LENGTH": "1048576",
        },
    }


def apply_settings_patch(patch, settings_path=SETTINGS_PATH):
    """Backs up settings_path if it exists (never a plain overwrite), then
    merges patch's mcpServers/env keys into it (never replaces the whole
    file), so an existing entry for a different MCP server or env var
    survives untouched. Returns (backed_up_to_or_None, written_path)."""
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    existing = {}
    backed_up_to = None
    if settings_path.exists():
        text = settings_path.read_text()
        try:
            existing = json.loads(text)
        except ValueError:
            raise ValueError(
                f"{settings_path} exists but isn't valid JSON — not touching it. Fix or back it up manually first."
            )
        if not isinstance(existing, dict):
            raise ValueError(f"{settings_path} isn't a JSON object — not touching it.")
        backup_path = settings_path.with_name(settings_path.name + f".bak-{int(time.time())}")
        backup_path.write_text(text)
        backed_up_to = str(backup_path)

    existing.setdefault("mcpServers", {})
    existing["mcpServers"].update(patch["mcpServers"])
    existing.setdefault("env", {})
    existing["env"].update(patch["env"])

    settings_path.write_text(json.dumps(existing, indent=2) + "\n")
    return backed_up_to, str(settings_path)


# Templated into a standalone script by /setup/local-script, with
# {base_url} and {bearer_token} substituted in at request time. Duplicates
# build_settings_patch/apply_settings_patch as plain code rather than
# importing this module, since the downloaded file has to run standalone
# on the user's machine with no mcp_context_inspector package installed —
# stdlib only (json, time, pathlib).
LOCAL_SCRIPT_TEMPLATE = '''#!/usr/bin/env python3
# This script contains your personal mcp-context-inspector token. Treat it
# like a password — don't share this file, and delete it after running.
#
# What it does: merges an MCP server entry (context-inspector, pointed at
# {base_url}) and OTLP telemetry env vars into your own
# ~/.claude/settings.json, so Claude Code connects to this server and
# reports live telemetry automatically in every session — no manual
# paste-in required. Your existing settings.json is backed up first and
# merged into, never overwritten.
#
# Safe to run more than once (re-applies the same values). Run with:
#     python3 {script_name}

import json
import time
from pathlib import Path

BASE_URL = {base_url!r}
BEARER_TOKEN = {bearer_token!r}
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

patch = {{
    "mcpServers": {{
        "context-inspector": {{
            "url": BASE_URL + "/mcp",
            "headers": {{"Authorization": "Bearer " + BEARER_TOKEN}},
        }},
    }},
    "env": {{
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
        "OTEL_EXPORTER_OTLP_ENDPOINT": BASE_URL + "/otlp",
        "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer " + BEARER_TOKEN,
        "OTEL_LOG_RAW_API_BODIES": "1",
        "CLAUDE_CODE_OTEL_CONTENT_MAX_LENGTH": "1048576",
    }},
}}

SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)

existing = {{}}
backed_up_to = None
if SETTINGS_PATH.exists():
    text = SETTINGS_PATH.read_text()
    try:
        existing = json.loads(text)
    except ValueError:
        raise SystemExit(
            f"{{SETTINGS_PATH}} exists but isn't valid JSON — not touching it. Fix or back it up manually first."
        )
    if not isinstance(existing, dict):
        raise SystemExit(f"{{SETTINGS_PATH}} isn't a JSON object — not touching it.")
    backup_path = SETTINGS_PATH.with_name(SETTINGS_PATH.name + f".bak-{{int(time.time())}}")
    backup_path.write_text(text)
    backed_up_to = str(backup_path)

existing.setdefault("mcpServers", {{}})
existing["mcpServers"].update(patch["mcpServers"])
existing.setdefault("env", {{}})
existing["env"].update(patch["env"])

SETTINGS_PATH.write_text(json.dumps(existing, indent=2) + "\\n")

print(f"Done — wrote to {{SETTINGS_PATH}}")
if backed_up_to:
    print(f"Previous version backed up to {{backed_up_to}}")
print("Restart any running Claude Code sessions to pick it up.")
print()
print("This script has no further use and still contains your token — delete it now:")
print(f"    rm {{__file__}}")
'''


def render_local_script(base_url, bearer_token, script_name="mcp-context-inspector-setup.py"):
    return LOCAL_SCRIPT_TEMPLATE.format(
        base_url=base_url.rstrip("/"),
        bearer_token=bearer_token,
        script_name=script_name,
    )
