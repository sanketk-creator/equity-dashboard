"""
The credentials the app is using *right now* — ported verbatim in structure
from the Nifty dashboard, so all four dashboards share one model:

  - the live token is held here (swap without redeploy),
  - expiry is read out of the token itself (IST, from auth.token_expiry),
  - automatic daily refresh works when DHAN_PIN and DHAN_TOTP_SECRET are set
    (see backend/auth.py — including its SECURITY note: PIN + TOTP seed
    together are your account login; they live only in Render's environment),
  - manual paste on Diagnostics remains fully supported as the fallback.

An UNSET admin password refuses writes — an unset password is not a blank one.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

from .auth import seconds_until_expiry, token_expiry


@dataclass
class Credentials:
    client_id: str = ""
    access_token: str = ""
    # where the current token came from: env | saved | auto
    source: str = "env"
    updated_at: float = 0.0
    last_refresh_error: str | None = None
    last_refresh_at: float | None = None

    # -- state --------------------------------------------------------------

    def set(self, client_id: str, access_token: str, source: str) -> None:
        self.client_id = (client_id or "").strip()
        self.access_token = (access_token or "").strip()
        self.source = source
        self.updated_at = time.time()

    def present(self) -> bool:
        return bool(self.client_id and self.access_token)

    # kept for call-site compatibility inside this repo
    @property
    def ok(self) -> bool:
        return self.present() and not self.expired()

    # -- expiry -------------------------------------------------------------

    def seconds_left(self) -> int | None:
        return seconds_until_expiry(self.access_token) if self.access_token else None

    def expired(self) -> bool:
        left = self.seconds_left()
        return left is not None and left <= 0

    def expires_within(self, seconds: int) -> bool:
        left = self.seconds_left()
        return left is not None and left <= seconds

    # -- display ------------------------------------------------------------

    def masked(self) -> str:
        """Never show the whole token back to the browser."""
        t = self.access_token
        if not t:
            return ""
        return f"…{t[-6:]}" if len(t) > 10 else "…"

    def status(self) -> dict[str, Any]:
        exp = token_expiry(self.access_token) if self.access_token else None
        left = self.seconds_left()
        return {
            "present": self.present(),
            "configured": self.present(),          # legacy key for the frontend
            "client_id": self.client_id,
            "token_tail": self.masked(),
            "source": self.source,
            "expires_at": exp.isoformat() if exp else None,
            "seconds_left": left,
            "hours_left": round(left / 3600, 1) if left is not None else None,
            "expires_in_hours": round(left / 3600, 1) if left is not None else None,
            "expired": self.expired(),
            "auto_refresh": auto_refresh_configured(),
            "last_refresh_at": self.last_refresh_at,
            "last_refresh_error": self.last_refresh_error,
        }


# The single shared instance, seeded from the environment at startup.
current = Credentials(
    client_id=os.getenv("DHAN_CLIENT_ID", "").strip(),
    access_token=os.getenv("DHAN_ACCESS_TOKEN", "").strip(),
    source="env",
)


# -- automatic refresh settings (all optional) ------------------------------

def dhan_pin() -> str:
    return os.getenv("DHAN_PIN", "").strip()


def totp_secret() -> str:
    return os.getenv("DHAN_TOTP_SECRET", "").strip()


def auto_refresh_configured() -> bool:
    return bool(current.client_id and dhan_pin() and totp_secret())


def admin_password() -> str:
    return os.getenv("ADMIN_PASSWORD", "").strip()
