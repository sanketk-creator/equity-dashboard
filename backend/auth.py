"""
Dhan access tokens expire every 24 hours.

Doing that by hand every morning before 9:15 is a chore you will eventually
forget, on a day that matters. So this module can generate a fresh token
automatically, using Dhan's direct token endpoint:

    POST https://auth.dhan.co/app/generateAccessToken
         ?dhanClientId=...&pin=...&totp=...

The TOTP is the same 6-digit code your authenticator app shows. Given the
secret seed behind that app, we can compute the current code ourselves -- so
the whole refresh happens with no human involved.

SECURITY, PLAINLY
-----------------
To do this the server needs your Dhan PIN and your TOTP seed. Together those
are your account login. They live in Render's environment variables (encrypted,
never in the repo), but you should understand the trade you're making before
switching this on. If you would rather not, leave it off and paste a token into
the dashboard each morning instead -- that path is fully supported and is the
default.

TOTP is implemented here from the RFC rather than pulled from a library, so
there is no extra dependency handling a secret.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import struct
import time
from datetime import datetime
from typing import Any

import httpx

from .config import IST

log = logging.getLogger("auth")

DHAN_AUTH_BASE = "https://auth.dhan.co"


# ---------------------------------------------------------------- TOTP

def totp_now(secret: str, at: int | None = None, digits: int = 6,
             period: int = 30, digest=hashlib.sha1) -> str:
    """
    The 6-digit code your authenticator app would be showing right now.

    RFC 6238. `secret` is the base32 string shown when you set up the
    authenticator (spaces and casing don't matter).
    """
    cleaned = secret.replace(" ", "").replace("-", "").upper()
    # base32 needs the length to be a multiple of 8; pad it.
    cleaned += "=" * (-len(cleaned) % 8)
    try:
        key = base64.b32decode(cleaned, casefold=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"TOTP secret is not valid base32: {exc}") from exc

    counter = int((at if at is not None else time.time()) // period)
    mac = hmac.new(key, struct.pack(">Q", counter), digest).digest()
    offset = mac[-1] & 0x0F
    code = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


# ------------------------------------------------------- token inspection

def token_expiry(access_token: str) -> datetime | None:
    """
    Read the expiry out of the token itself.

    Dhan issues a JWT. The middle section is base64 JSON containing an `exp`
    claim, so we can tell you exactly when it dies rather than guessing. We only
    read it -- no signature check, because we're not the one validating it.

    Returned in IST, explicitly. This used to be `fromtimestamp(exp)` with no
    timezone, which meant the CONTAINER's local zone -- UTC on Render. Every
    other time on the dashboard is IST, so the expiry read 5h30m early with
    nothing to indicate a different zone: a token showing "expired at 10:18"
    at 11:27 looked dead when it had four hours left. That is the wrong
    direction for the one number you check before the market opens, because it
    invites regenerating a working token, and generating a new token can
    invalidate the one the other dashboard is using.
    """
    try:
        parts = access_token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get("exp")
        return datetime.fromtimestamp(int(exp), IST) if exp else None
    except Exception:
        return None


def seconds_until_expiry(access_token: str) -> int | None:
    exp = token_expiry(access_token)
    if exp is None:
        return None
    return int(exp.timestamp() - time.time())


# ---------------------------------------------------------- token refresh

class TokenRefreshError(RuntimeError):
    pass


async def fetch_access_token(client_id: str, pin: str, totp_secret: str,
                             timeout: float = 20.0) -> dict[str, Any]:
    """
    Ask Dhan for a fresh 24-hour access token.

    Returns {"access_token", "expiry_time", "client_name"}.
    """
    if not (client_id and pin and totp_secret):
        raise TokenRefreshError("client id, PIN and TOTP secret are all required")

    code = totp_now(totp_secret)
    params = {"dhanClientId": client_id, "pin": pin, "totp": code}

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(f"{DHAN_AUTH_BASE}/app/generateAccessToken", params=params)

    if resp.status_code in (400, 401, 403):
        # Don't echo the body blindly -- it can contain identifying details.
        raise TokenRefreshError(
            f"Dhan rejected the login (HTTP {resp.status_code}). "
            "Check the PIN, and that the TOTP secret matches the authenticator "
            "entry for this Dhan account."
        )
    if resp.status_code >= 500:
        raise TokenRefreshError(f"Dhan auth service error (HTTP {resp.status_code}); try again shortly.")
    resp.raise_for_status()

    try:
        data = resp.json()
    except Exception as exc:
        raise TokenRefreshError(f"Unreadable response from Dhan auth: {exc}") from exc

    token = data.get("accessToken")
    if not token:
        raise TokenRefreshError(f"No access token in the response: {str(data)[:200]}")

    log.info("Fetched a fresh Dhan access token (expires %s)", data.get("expiryTime"))
    return {
        "access_token": token,
        "expiry_time": data.get("expiryTime"),
        "client_name": data.get("dhanClientName"),
    }
