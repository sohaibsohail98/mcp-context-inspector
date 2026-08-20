"""Backend dispatcher for the per-user MCP token store. Same
STORAGE_BACKEND env var as metrics/store.py, so sessions and auth always
switch backends together. SQLite for local dev, DynamoDB once deployed
with STORAGE_BACKEND=dynamodb set. Same function signatures either way;
callers import from here and never know which backend is active.

Identity/credential data (Google sign-in tokens, OAuth client
registrations, OAuth codes and tokens) — see
mcp_server/auth/store_sqlite.py's module docstring for the full shape
and the OAuth 2.1 + PKCE flow this backs.
"""

import os

if os.environ.get("STORAGE_BACKEND", "sqlite") == "dynamodb":
    from mcp_server.auth.store_dynamodb import (
        get_oauth_client,
        get_or_create_token,
        get_sub_for_token,
        is_valid_token,
        issue_oauth_code,
        list_oauth_clients,
        list_oauth_tokens,
        list_users,
        mint_oauth_token,
        redeem_oauth_code,
        register_oauth_client,
        revoke,
        revoke_oauth_client,
        revoke_oauth_token,
    )
else:
    from mcp_server.auth.store_sqlite import (
        get_oauth_client,
        get_or_create_token,
        get_sub_for_token,
        is_valid_token,
        issue_oauth_code,
        list_oauth_clients,
        list_oauth_tokens,
        list_users,
        mint_oauth_token,
        redeem_oauth_code,
        register_oauth_client,
        revoke,
        revoke_oauth_client,
        revoke_oauth_token,
    )

__all__ = [
    "get_oauth_client",
    "get_or_create_token",
    "get_sub_for_token",
    "is_valid_token",
    "issue_oauth_code",
    "list_oauth_clients",
    "list_oauth_tokens",
    "list_users",
    "mint_oauth_token",
    "redeem_oauth_code",
    "register_oauth_client",
    "revoke",
    "revoke_oauth_client",
    "revoke_oauth_token",
]
