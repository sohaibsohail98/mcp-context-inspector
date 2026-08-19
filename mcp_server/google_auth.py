"""Verifies a Google Identity Services ID token (a signed JWT handed to
`/auth/verify` by the browser after "Sign in with Google") — the lighter
half of the hybrid auth model documented in the README. Deliberately not
a full OAuth 2.1 authorization-server build: no client secret, no
redirect URIs, no authorization-code exchange. Google Identity
Services' one-tap/button flow hands the frontend a signed JWT directly;
verifying it server-side (signature + audience + issuer, all handled by
google-auth's own library, not hand-rolled) is enough to know which
real Google account is asking, at a fraction of the infrastructure a
full authorization server would need.
"""

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

_google_request = google_requests.Request()


class InvalidGoogleToken(Exception):
    pass


def verify_credential(credential, client_id):
    """Returns {"sub": ..., "email": ...} for a valid, signed Google ID
    token whose audience matches this server's OAuth client ID. Raises
    InvalidGoogleToken on anything else (expired, wrong audience, bad
    signature, malformed) — verify_oauth2_token already checks
    signature/expiry/issuer against Google's published keys; this only
    adds the audience check via the `client_id` argument and normalizes
    the error type for callers."""
    try:
        payload = id_token.verify_oauth2_token(credential, _google_request, client_id)
    except ValueError as e:
        raise InvalidGoogleToken(str(e)) from e

    return {"sub": payload["sub"], "email": payload.get("email")}
