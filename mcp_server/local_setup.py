"""Shared merge/backup logic for writing this account's MCP connection +
OTLP telemetry config into a Claude Code ~/.claude/settings.json. Used by
both:

- The /setup/apply-local-config route, when the server itself is running
  on the caller's own machine (self-hosted, loopback).
- The downloadable script templated by /setup/local-script (see
  LOCAL_SCRIPT_TEMPLATE below), when the server is deployed elsewhere and
  can't reach the caller's filesystem directly. The script runs this same
  logic locally instead.
"""

import json
import time
from pathlib import Path

SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

# Shared by routes/setup.py (reports this in /setup/issue-install-code's
# response) and every auth/store_*.py backend's issue_install_code
# default. One source of truth for the "Pin TTL to <=5 minutes" rule
# from CTXWINDOW_LAUNCH_PLAN.md, rather than three copies that could drift.
INSTALL_CODE_TTL_SECONDS = 300


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
            # Claude Code's exporter never sets service.name itself, and
            # that is the primary signal mcp_server/otlp/__init__.py's
            # detect_vendor() matches on. Without this, every session falls back
            # to detect_vendor's session.id-presence check, which is itself
            # opt-in and was silently missing both here and in what a real
            # Claude Code session sends by default. Confirmed against a
            # real captured session on 2026-08-25 that all four of these
            # are what actually got a session to land.
            "OTEL_RESOURCE_ATTRIBUTES": "service.name=claude-code",
            "OTEL_METRICS_INCLUDE_SESSION_ID": "true",
            "OTEL_LOGS_INCLUDE_SESSION_ID": "true",
            "OTEL_LOGS_EXPORT_INTERVAL": "5000",
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
        except ValueError as err:
            raise ValueError(
                f"{settings_path} exists but isn't valid JSON. Not touching it. Fix or back it up manually first."
            ) from err
        if not isinstance(existing, dict):
            raise ValueError(f"{settings_path} isn't a JSON object. Not touching it.")
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
# on the user's machine with no mcp_context_inspector package installed:
# stdlib only (json, time, pathlib).
LOCAL_SCRIPT_TEMPLATE = '''#!/usr/bin/env python3
# This script contains your personal mcp-context-inspector token. Treat it
# like a password: don't share this file, and delete it after running.
#
# What it does: merges an MCP server entry (context-inspector, pointed at
# {base_url}) and OTLP telemetry env vars into your own
# ~/.claude/settings.json, so Claude Code connects to this server and
# reports live telemetry automatically in every session, with no manual
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
        "OTEL_RESOURCE_ATTRIBUTES": "service.name=claude-code",
        "OTEL_METRICS_INCLUDE_SESSION_ID": "true",
        "OTEL_LOGS_INCLUDE_SESSION_ID": "true",
        "OTEL_LOGS_EXPORT_INTERVAL": "5000",
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
            f"{{SETTINGS_PATH}} exists but isn't valid JSON. Not touching it. Fix or back it up manually first."
        )
    if not isinstance(existing, dict):
        raise SystemExit(f"{{SETTINGS_PATH}} isn't a JSON object. Not touching it.")
    backup_path = SETTINGS_PATH.with_name(SETTINGS_PATH.name + f".bak-{{int(time.time())}}")
    backup_path.write_text(text)
    backed_up_to = str(backup_path)

existing.setdefault("mcpServers", {{}})
existing["mcpServers"].update(patch["mcpServers"])
existing.setdefault("env", {{}})
existing["env"].update(patch["env"])

SETTINGS_PATH.write_text(json.dumps(existing, indent=2) + "\\n")

print(f"Done. Wrote to {{SETTINGS_PATH}}")
if backed_up_to:
    print(f"Previous version backed up to {{backed_up_to}}")
print("Restart any running Claude Code sessions to pick it up.")
print()
print("This script has no further use and still contains your token. Delete it now:")
print(f"    rm {{__file__}}")
'''


def render_local_script(base_url, bearer_token, script_name="mcp-context-inspector-setup.py"):
    return LOCAL_SCRIPT_TEMPLATE.format(
        base_url=base_url.rstrip("/"),
        bearer_token=bearer_token,
        script_name=script_name,
    )


# The curl-able one-liner served by GET /setup/install (see
# routes/setup.py). Must be POSIX `sh`, not bash: the piped form
# (`curl ... | sh`) invokes `sh` explicitly, so the shebang line below is
# never consulted. Only the body's actual syntax matters, and `sh` on
# some platforms (Debian/Ubuntu's dash, unlike bash) rejects bash-only
# constructs like `[[ ]]` or arrays.
#
# Rather than reimplement the JSON backup/merge logic in shell, which
# would be a second, drifting copy of the same logic, this script shells
# out to `python3 -c` with the identical merge code
# LOCAL_SCRIPT_TEMPLATE already carries, so both delivery paths
# (piped-curl and downloaded-script) execute the literal same Python.
# python3 is a stated dependency, not a silent assumption: the script
# checks for it first and fails with a clear message otherwise.
INSTALL_SHELL_TEMPLATE = '''#!/bin/sh
# mcp-context-inspector installer. Safe to re-run (idempotent merge of
# your ~/.claude/settings.json, never a plain overwrite; your existing
# file is backed up first). Not comfortable piping into a shell? Save
# this to a file first and read it before running:
#     curl -fsSL {base_url}/setup/install?t=<code> -o install.sh
#     less install.sh
#     sh install.sh

set -e

if ! command -v python3 >/dev/null 2>&1; then
    echo "mcp-context-inspector: python3 is required but wasn't found on PATH." >&2
    echo "Install Python 3, then re-run this command." >&2
    exit 1
fi

# BASE_URL/BEARER_TOKEN travel as environment variables, never
# interpolated into the double-quoted python3 -c "..." body below.
# A value containing $, a backtick, or a backslash
# would otherwise be expanded/executed by sh before Python ever saw it
# (verified: a token containing a backtick-wrapped command executed on
# the way in). MCP_INSTALL_BASE_URL/MCP_INSTALL_BEARER_TOKEN are this
# script's own private env vars, scoped to this one command only.
MCP_INSTALL_BASE_URL={base_url_shell_quoted} \\
MCP_INSTALL_BEARER_TOKEN={bearer_token_shell_quoted} \\
python3 -c "
import json
import os
import time
from pathlib import Path

BASE_URL = os.environ['MCP_INSTALL_BASE_URL']
BEARER_TOKEN = os.environ['MCP_INSTALL_BEARER_TOKEN']
SETTINGS_PATH = Path.home() / '.claude' / 'settings.json'

patch = {{
    'mcpServers': {{
        'context-inspector': {{
            'url': BASE_URL + '/mcp',
            'headers': {{'Authorization': 'Bearer ' + BEARER_TOKEN}},
        }},
    }},
    'env': {{
        'CLAUDE_CODE_ENABLE_TELEMETRY': '1',
        'OTEL_LOGS_EXPORTER': 'otlp',
        'OTEL_METRICS_EXPORTER': 'otlp',
        'OTEL_EXPORTER_OTLP_PROTOCOL': 'http/json',
        'OTEL_EXPORTER_OTLP_ENDPOINT': BASE_URL + '/otlp',
        'OTEL_EXPORTER_OTLP_HEADERS': 'Authorization=Bearer ' + BEARER_TOKEN,
        'OTEL_LOG_RAW_API_BODIES': '1',
        'CLAUDE_CODE_OTEL_CONTENT_MAX_LENGTH': '1048576',
        'OTEL_RESOURCE_ATTRIBUTES': 'service.name=claude-code',
        'OTEL_METRICS_INCLUDE_SESSION_ID': 'true',
        'OTEL_LOGS_INCLUDE_SESSION_ID': 'true',
        'OTEL_LOGS_EXPORT_INTERVAL': '5000',
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
        raise SystemExit(f'{{SETTINGS_PATH}} exists but isn\\'t valid JSON. Not touching it. Fix or back it up manually first.')
    if not isinstance(existing, dict):
        raise SystemExit(f'{{SETTINGS_PATH}} isn\\'t a JSON object. Not touching it.')
    backup_path = SETTINGS_PATH.with_name(SETTINGS_PATH.name + f'.bak-{{int(time.time())}}')
    backup_path.write_text(text)
    backed_up_to = str(backup_path)

existing.setdefault('mcpServers', {{}})
existing['mcpServers'].update(patch['mcpServers'])
existing.setdefault('env', {{}})
existing['env'].update(patch['env'])

SETTINGS_PATH.write_text(json.dumps(existing, indent=2) + chr(10))

print(f'Done. Wrote to {{SETTINGS_PATH}}')
if backed_up_to:
    print(f'Previous version backed up to {{backed_up_to}}')
"

echo ""
echo "Close any running Claude Code sessions (terminal windows or editor"
echo "integrations) and open a fresh one. Env vars only load once at"
echo "process startup, so an already-open session won't pick this up."
echo ""
echo "Then run one prompt and check \\"Test my connection\\" on the page."
'''


def _sh_single_quote(value):
    """POSIX sh single-quoting: nothing is special inside '...' except a
    literal single quote itself, which can't be escaped from inside the
    quotes at all. The standard trick is to close the quote, emit an
    escaped literal quote, then reopen: ' -> '\\''. This is what actually
    closes the shell-injection gap described in INSTALL_SHELL_TEMPLATE's
    comment: env-var assignment syntax (`NAME=value cmd`) is still
    shell-parsed, so the value needs real shell quoting, not just
    Python's repr()."""
    return "'" + value.replace("'", "'\\''") + "'"


def render_install_shell_script(base_url, bearer_token):
    return INSTALL_SHELL_TEMPLATE.format(
        base_url=base_url.rstrip("/"),
        bearer_token=bearer_token,
        base_url_shell_quoted=_sh_single_quote(base_url.rstrip("/")),
        bearer_token_shell_quoted=_sh_single_quote(bearer_token),
    )


# The `irm .../setup/install?os=windows | iex` one-liner's target (see
# routes/setup.py). PowerShell 5.1 (ships in Windows 10/11) and 7+ both
# run this. Mirrors INSTALL_SHELL_TEMPLATE exactly: it does NOT
# reimplement the JSON backup/merge in PowerShell (a second, drifting
# copy) -- it shells out to `python -c` with the identical merge body
# the sh installer already carries. python is a stated dependency: the
# script checks for it (python, then python3) and fails with a clear
# message otherwise.
#
# BASE_URL / BEARER_TOKEN travel as process env vars
# ($env:MCP_INSTALL_BASE_URL / $env:MCP_INSTALL_BEARER_TOKEN), never
# interpolated into the double-quoted python -c body, so a token
# containing $, a backtick, or a quote can't be expanded or executed by
# PowerShell on the way in (the same threat _sh_single_quote guards
# against for sh). The two values are emitted as single-quoted
# PowerShell literals; the only metacharacter inside a PowerShell
# single-quoted string is `'` itself, escaped by doubling it.
INSTALL_POWERSHELL_TEMPLATE = r'''# mcp-context-inspector installer (Windows / PowerShell).
# Safe to re-run: idempotent merge of your %USERPROFILE%\.claude\settings.json,
# never a plain overwrite; your existing file is backed up first.
# Not comfortable piping into iex? Download and read it first:
#     irm "{base_url}/setup/install?os=windows&t=<code>" -OutFile install.ps1
#     Get-Content install.ps1
#     .\install.ps1

$ErrorActionPreference = "Stop"

$py = $null
foreach ($cand in @("python", "python3")) {{
    if (Get-Command $cand -ErrorAction SilentlyContinue) {{ $py = $cand; break }}
}}
if (-not $py) {{
    Write-Error "mcp-context-inspector: Python 3 is required but wasn't found on PATH. Install it from python.org, reopen PowerShell, then re-run this command."
    exit 1
}}

$env:MCP_INSTALL_BASE_URL = {base_url_ps_quoted}
$env:MCP_INSTALL_BEARER_TOKEN = {bearer_token_ps_quoted}

& $py -c "
import json
import os
import time
from pathlib import Path

BASE_URL = os.environ['MCP_INSTALL_BASE_URL']
BEARER_TOKEN = os.environ['MCP_INSTALL_BEARER_TOKEN']
SETTINGS_PATH = Path.home() / '.claude' / 'settings.json'

patch = {{
    'mcpServers': {{
        'context-inspector': {{
            'url': BASE_URL + '/mcp',
            'headers': {{'Authorization': 'Bearer ' + BEARER_TOKEN}},
        }},
    }},
    'env': {{
        'CLAUDE_CODE_ENABLE_TELEMETRY': '1',
        'OTEL_LOGS_EXPORTER': 'otlp',
        'OTEL_METRICS_EXPORTER': 'otlp',
        'OTEL_EXPORTER_OTLP_PROTOCOL': 'http/json',
        'OTEL_EXPORTER_OTLP_ENDPOINT': BASE_URL + '/otlp',
        'OTEL_EXPORTER_OTLP_HEADERS': 'Authorization=Bearer ' + BEARER_TOKEN,
        'OTEL_LOG_RAW_API_BODIES': '1',
        'CLAUDE_CODE_OTEL_CONTENT_MAX_LENGTH': '1048576',
        'OTEL_RESOURCE_ATTRIBUTES': 'service.name=claude-code',
        'OTEL_METRICS_INCLUDE_SESSION_ID': 'true',
        'OTEL_LOGS_INCLUDE_SESSION_ID': 'true',
        'OTEL_LOGS_EXPORT_INTERVAL': '5000',
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
        raise SystemExit(f'{{SETTINGS_PATH}} exists but is not valid JSON. Not touching it. Fix or back it up manually first.')
    if not isinstance(existing, dict):
        raise SystemExit(f'{{SETTINGS_PATH}} is not a JSON object. Not touching it.')
    backup_path = SETTINGS_PATH.with_name(SETTINGS_PATH.name + f'.bak-{{int(time.time())}}')
    backup_path.write_text(text)
    backed_up_to = str(backup_path)

existing.setdefault('mcpServers', {{}})
existing['mcpServers'].update(patch['mcpServers'])
existing.setdefault('env', {{}})
existing['env'].update(patch['env'])

SETTINGS_PATH.write_text(json.dumps(existing, indent=2) + chr(10))

print(f'Done. Wrote to {{SETTINGS_PATH}}')
if backed_up_to:
    print(f'Previous version backed up to {{backed_up_to}}')
"

Write-Host ""
Write-Host "Close any running Claude Code sessions (terminal windows or editor"
Write-Host "integrations) and open a fresh one. Env vars only load once at"
Write-Host "process startup, so an already-open session won't pick this up."
Write-Host ""
Write-Host "Then run one prompt and check 'Test my connection' on the page."
'''


def _ps_single_quote(value):
    """PowerShell single-quoted string literal: the only special
    character inside '...' is the single quote itself, escaped by
    doubling it ('' -> one literal '). Same role as _sh_single_quote for
    the sh installer: `$env:NAME = value` is still parsed by PowerShell,
    so the value needs real PowerShell quoting, not repr()."""
    return "'" + value.replace("'", "''") + "'"


def render_install_powershell_script(base_url, bearer_token):
    return INSTALL_POWERSHELL_TEMPLATE.format(
        base_url=base_url.rstrip("/"),
        base_url_ps_quoted=_ps_single_quote(base_url.rstrip("/")),
        bearer_token_ps_quoted=_ps_single_quote(bearer_token),
    )
