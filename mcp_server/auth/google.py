"""Verifies a Google Identity Services ID token (a signed JWT handed to
`/auth/verify` by the browser after "Sign in with Google"). This is not
a full OAuth 2.1 authorization server: no client secret, no redirect
URIs, no authorization-code exchange. Google Identity Services' one-tap/
button flow hands the frontend a signed JWT directly; verifying it
server-side (signature + audience + issuer, via google-auth's library)
is enough to know which real Google account is asking.
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
    signature, malformed)."""
    try:
        payload = id_token.verify_oauth2_token(credential, _google_request, client_id)
    except ValueError as e:
        raise InvalidGoogleToken(str(e)) from e

    # verify_oauth2_token leaves email-verification enforcement to the
    # caller. Enforce it here when an email is present, since it's used
    # as a display/attribution value (dashboard identity, OAuth token
    # records). No `email` claim at all just means the scope wasn't
    # granted, which is fine — nothing to verify, so it isn't rejected.
    if payload.get("email") and not payload.get("email_verified"):
        raise InvalidGoogleToken("Google account email is not verified")

    return {"sub": payload["sub"], "email": payload.get("email")}
