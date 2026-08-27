"""Developer-mode allowlist. Gates visibility of synthetic test data
(api_tests/'s live-suite probe sessions, session_id prefix "api-tests-")
behind a small, explicit list of Google `sub` values, so the normal
dashboard never shows internal test traffic to anyone by default, but
the maintainer (or anyone else explicitly added) can still see it when
debugging. Keyed on `sub`, not email, since current_owner already IS
the sub, so no extra lookup is needed. Set via DEV_MODE_SUBS, a comma-
separated list of Google `sub` values (find your own via
mcp_server.auth.store.list_users())."""

import os


def _allowed_subs():
    raw = os.environ.get("DEV_MODE_SUBS", "")
    return {s.strip() for s in raw.split(",") if s.strip()}


def is_dev_mode_account(owner):
    """owner: the value from current_owner.get(), a Google sub, or None
    for the shared owner token. The owner token is always allowed (it's
    the maintainer's own server); a per-user sub is allowed only if it's
    in DEV_MODE_SUBS."""
    if owner is None:
        return True
    return owner in _allowed_subs()
