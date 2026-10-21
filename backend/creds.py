"""Dhan credentials + the dashboard admin password. Same conventions as the
options dashboards: token renewable from the Diagnostics page (Dhan tokens
expire daily), secrets only ever via environment or the token endpoint, and
an UNSET admin password refuses writes — an unset password is not a blank one."""
from __future__ import annotations

import base64
import json
import time

from . import config


class Credentials:
    def __init__(self):
        self.client_id = config.DHAN_CLIENT_ID
        self.access_token = config.DHAN_ACCESS_TOKEN
        self.source = "env" if self.access_token else "missing"
        self.last_refresh_at: float | None = None

    def set(self, client_id: str, token: str, source: str = "saved") -> None:
        if client_id:
            self.client_id = client_id
        self.access_token = token
        self.source = source
        self.last_refresh_at = time.time()

    @property
    def ok(self) -> bool:
        return bool(self.client_id and self.access_token)

    def seconds_until_expiry(self) -> float | None:
        """Dhan tokens are JWTs with an exp claim."""
        tok = self.access_token
        if not tok or tok.count(".") != 2:
            return None
        try:
            payload = tok.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
            return float(exp) - time.time() if exp else None
        except Exception:
            return None

    def status(self) -> dict:
        left = self.seconds_until_expiry()
        return {"configured": self.ok, "source": self.source,
                "expires_in_hours": round(left / 3600, 1) if left else None,
                "expired": (left is not None and left <= 0)}


current = Credentials()


def admin_password() -> str:
    return config.ADMIN_PASSWORD
