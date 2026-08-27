"""Backend dispatcher for the per-user MCP token store. Same
STORAGE_BACKEND env var as metrics/store.py, so sessions and auth always
switch backends together. SQLite for local dev, DynamoDB or Firestore
once deployed (see README.md's "Storage backends"; under SQLite a lost
token row on cold start silently breaks that user's auth). Same function
signatures either way; callers import from here and never know which is
active.

See mcp_server/auth/store_sqlite.py's module docstring for the data
shape and the OAuth 2.1 + PKCE flow this backs.
"""

import os

_backend = os.environ.get("STORAGE_BACKEND", "sqlite")

if _backend == "dynamodb":
    from mcp_server.auth.store_dynamodb import (
        get_oauth_client,
        get_or_create_token,
        get_sub_for_token,
        is_valid_token,
        issue_install_code,
        issue_oauth_code,
        list_oauth_clients,
        list_oauth_tokens,
        list_users,
        mint_oauth_token,
        redeem_install_code,
        redeem_oauth_code,
        register_oauth_client,
        revoke,
        revoke_oauth_client,
        revoke_oauth_token,
    )
elif _backend == "firestore":
    from mcp_server.auth.store_firestore import (
        get_oauth_client,
        get_or_create_token,
        get_sub_for_token,
        is_valid_token,
        issue_install_code,
        issue_oauth_code,
        list_oauth_clients,
        list_oauth_tokens,
        list_users,
        mint_oauth_token,
        redeem_install_code,
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
        issue_install_code,
        issue_oauth_code,
        list_oauth_clients,
        list_oauth_tokens,
        list_users,
        mint_oauth_token,
        redeem_install_code,
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
    "issue_install_code",
    "issue_oauth_code",
    "list_oauth_clients",
    "list_oauth_tokens",
    "list_users",
    "mint_oauth_token",
    "redeem_install_code",
    "redeem_oauth_code",
    "register_oauth_client",
    "revoke",
    "revoke_oauth_client",
    "revoke_oauth_token",
]
