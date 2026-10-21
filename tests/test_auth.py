"""
Tests for token handling.

The TOTP cases use the official RFC 6238 test vectors. If these pass, the codes
we generate are the same ones your authenticator app shows -- which is the whole
basis of the automatic daily refresh.
"""
from __future__ import annotations

import base64
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.auth import seconds_until_expiry, token_expiry, totp_now
from backend.config import IST
from backend.creds import Credentials

# RFC 6238 uses the ASCII seed "12345678901234567890" for SHA-1.
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()


def test_totp_matches_rfc6238_vectors():
    # (unix time, expected 8-digit code) from the RFC's SHA-1 table
    for ts, expected8 in [
        (59, "94287082"),
        (1111111109, "07081804"),
        (1111111111, "14050471"),
        (1234567890, "89005924"),
        (2000000000, "69279037"),
        (20000000000, "65353130"),
    ]:
        got = totp_now(RFC_SECRET, at=ts, digits=8)
        assert got == expected8, f"t={ts}: expected {expected8}, got {got}"


def test_totp_is_six_digits_by_default():
    code = totp_now(RFC_SECRET, at=59)
    assert len(code) == 6 and code.isdigit()
    assert code == "287082"          # last 6 of the 8-digit vector


def test_totp_changes_every_30_seconds():
    # Windows are floor(t / 30), so they start at multiples of 30 --
    # 990-1019 is one window, 1020-1049 the next.
    assert totp_now(RFC_SECRET, at=990) == totp_now(RFC_SECRET, at=1019)
    assert totp_now(RFC_SECRET, at=1019) != totp_now(RFC_SECRET, at=1020)
    assert totp_now(RFC_SECRET, at=1020) == totp_now(RFC_SECRET, at=1049)


def test_totp_tolerates_spaces_and_lowercase():
    """Authenticator setup screens show the seed in spaced lowercase groups."""
    spaced = " ".join(RFC_SECRET[i:i + 4] for i in range(0, len(RFC_SECRET), 4)).lower()
    assert totp_now(spaced, at=59) == totp_now(RFC_SECRET, at=59)


def test_totp_rejects_a_bad_secret():
    for bad in ("not base32!!", "18890"):
        try:
            totp_now(bad)
            raise AssertionError(f"should have rejected {bad!r}")
        except ValueError:
            pass


# ------------------------------------------------------ expiry parsing

def _jwt(exp: int) -> str:
    import json
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{b64({'alg': 'HS256'})}.{b64({'exp': exp, 'dhanClientId': '100'})}.sig"


def test_reads_expiry_out_of_the_token():
    exp = int(time.time()) + 3600
    assert abs(token_expiry(_jwt(exp)).timestamp() - exp) < 2
    left = seconds_until_expiry(_jwt(exp))
    assert 3500 < left <= 3600


def test_expiry_of_a_malformed_token_is_unknown_not_a_crash():
    for bad in ("", "not-a-jwt", "a.b", "a.!!!.c"):
        assert token_expiry(bad) is None
        assert seconds_until_expiry(bad) is None


def test_expired_token_reports_negative_time():
    assert seconds_until_expiry(_jwt(int(time.time()) - 60)) < 0


def test_expiry_is_returned_in_ist_not_the_container_zone():
    """
    Render's container runs in UTC. A naive `fromtimestamp` therefore produced
    an expiry 5h30m behind every other clock on the dashboard, with no offset
    to signal it. Pin the zone so the number cannot drift with the host.
    """
    exp = int(time.time()) + 3600
    got = token_expiry(_jwt(exp))

    assert got.tzinfo is not None, "expiry must be timezone-aware, not naive"
    assert got.utcoffset().total_seconds() == 5.5 * 3600, "expiry must be IST (+05:30)"
    # Same instant, and the same wall clock a Mumbai trader would read.
    assert abs(got.timestamp() - exp) < 2
    assert got.strftime("%Y-%m-%dT%H:%M") == \
        datetime.fromtimestamp(exp, IST).strftime("%Y-%m-%dT%H:%M")


def test_displayed_expiry_agrees_with_hours_left():
    """
    The bug that mattered: `expires_at` and `hours_left` disagreed by 5h30m, so
    a live token read as dead. Whatever the host zone, the timestamp shown must
    land where `seconds_left` says it will.
    """
    left = 4 * 3600 + 1800
    status = Credentials("100", _jwt(int(time.time()) + left), source="saved").status()

    shown = datetime.fromisoformat(status["expires_at"])
    assert shown.tzinfo is not None, "expires_at must carry an offset, or it reads as local time"

    gap = (shown - datetime.now(IST)).total_seconds()
    assert abs(gap - status["seconds_left"]) < 2, (
        f"expires_at implies {gap / 3600:.2f}h but seconds_left says "
        f"{status['seconds_left'] / 3600:.2f}h"
    )
    assert status["hours_left"] == 4.5
    assert not status["expired"]


# ------------------------------------------------------ credential state

def test_credentials_track_expiry_state():
    c = Credentials()
    c.set("100", _jwt(int(time.time()) + 7200), source="saved")
    assert c.present() and not c.expired()
    assert c.expires_within(3 * 3600)
    assert not c.expires_within(60)

    c.set("100", _jwt(int(time.time()) - 10), source="saved")
    assert c.expired()


def test_token_is_never_returned_in_full():
    c = Credentials()
    full = _jwt(int(time.time()) + 3600)
    c.set("100", full, source="saved")
    status = c.status()
    assert full not in str(status), "the full token must never reach the browser"
    assert status["token_tail"].startswith("…")
    assert len(status["token_tail"]) <= 8


def test_empty_credentials_are_not_present():
    c = Credentials()
    assert not c.present()
    assert c.seconds_left() is None
    assert c.masked() == ""


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                passed += 1
                print(f"  PASS  {name}")
            except Exception as exc:
                failed += 1
                print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
