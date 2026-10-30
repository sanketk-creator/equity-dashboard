"""
NSE trading calendar (spec §7.5, §11.6).

"Last session of the ISO week" and "last session of the month" are computed
from this calendar, never from weekday arithmetic. A session is a weekday
that is not a trading holiday, OR a declared special session (Muhurat,
occasional Saturday). Special sessions ARE trading sessions.

The calendar is only trusted through the last date it is known for
(COVERED_THROUGH). Past that, a decision that depends on "is this the last
session of the week/month" refuses to run instead of guessing.

Seed: NSE trading holidays 2026 as supplied by the operator (30 Oct 2026),
plus the Muhurat session of Sunday 8 Nov 2026 (14:20-15:30, NSE/BSE
announcement). Edited on the dashboard; the operator adds 2027 on 1 Jan.
"""
from __future__ import annotations

from datetime import date, timedelta

HOLIDAYS_2026 = {
    "2026-01-15": "Municipal Corporation Election - Maharashtra",
    "2026-01-26": "Republic Day",
    "2026-03-03": "Holi",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-10": "Diwali-Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}
SPECIAL_2026 = {
    "2026-11-08": "Muhurat trading (Diwali Laxmi Pujan), 14:20-15:30",
}
COVERED_THROUGH_SEED = "2026-12-31"


class CalendarGap(RuntimeError):
    """A date beyond the calendar's coverage was needed."""


class NSECalendar:
    def __init__(self, holidays: dict[str, str], special: dict[str, str],
                 covered_through: str):
        self.holidays = dict(holidays)
        self.special = dict(special)
        self.covered_through = covered_through

    @classmethod
    def seed(cls) -> "NSECalendar":
        return cls(HOLIDAYS_2026, SPECIAL_2026, COVERED_THROUGH_SEED)

    def to_record(self) -> dict:
        return {"holidays": self.holidays, "special": self.special,
                "covered_through": self.covered_through}

    @classmethod
    def from_record(cls, r: dict) -> "NSECalendar":
        return cls(r.get("holidays") or {}, r.get("special") or {},
                   r.get("covered_through") or COVERED_THROUGH_SEED)

    # ------------------------------------------------------------------

    def covers(self, d: str) -> bool:
        return d <= self.covered_through

    def _need(self, d: str) -> None:
        if not self.covers(d):
            raise CalendarGap(f"NSE calendar not entered beyond "
                              f"{self.covered_through} (needed {d})")

    def is_session(self, d: str) -> bool:
        self._need(d)
        if d in self.special:
            return True
        if d in self.holidays:
            return False
        return date.fromisoformat(d).weekday() < 5

    def is_special(self, d: str) -> bool:
        return d in self.special

    def next_session(self, d: str) -> str:
        x = date.fromisoformat(d)
        for _ in range(40):
            x += timedelta(days=1)
            s = x.isoformat()
            if self.is_session(s):
                return s
        raise CalendarGap(f"no session within 40 days after {d}")

    def prev_session(self, d: str) -> str:
        x = date.fromisoformat(d)
        for _ in range(40):
            x -= timedelta(days=1)
            s = x.isoformat()
            if s <= self.covered_through and self.is_session(s):
                return s
        raise CalendarGap(f"no session within 40 days before {d}")

    def sessions_between(self, after: str, before: str) -> list[str]:
        """Sessions strictly after `after` and strictly before `before`."""
        out, x = [], date.fromisoformat(after)
        end = date.fromisoformat(before)
        while True:
            x += timedelta(days=1)
            if x >= end:
                return out
            s = x.isoformat()
            if self.is_session(s):
                out.append(s)

    def is_last_session_of_iso_week(self, d: str) -> bool:
        """True when no later session falls in the same ISO week (Mon-Sun)."""
        if not self.is_session(d):
            return False
        x = date.fromisoformat(d)
        wk = x.isocalendar()[:2]
        y = x + timedelta(days=1)
        while y.isocalendar()[:2] == wk:
            if self.is_session(y.isoformat()):
                return False
            y += timedelta(days=1)
        return True

    def is_last_session_of_month(self, d: str) -> bool:
        if not self.is_session(d):
            return False
        x = date.fromisoformat(d)
        y = x + timedelta(days=1)
        while y.month == x.month:
            if self.is_session(y.isoformat()):
                return False
            y += timedelta(days=1)
        return True

    def reminder(self, today: str) -> str | None:
        """Banner text from 15 Dec until next year's list is entered."""
        t = date.fromisoformat(today)
        year_end = f"{t.year}-12-31"
        if self.covered_through >= f"{t.year + 1}-12-31":
            return None
        if today > self.covered_through:
            return (f"NSE CALENDAR MISSING after {self.covered_through} — "
                    f"Strategies 2 and 3 cannot decide week-/month-end "
                    f"sessions. Enter the {t.year} trading holidays now.")
        if t.month == 12 and t.day >= 15 and self.covered_through <= year_end:
            return (f"Reminder: enter the {t.year + 1} NSE trading holidays "
                    f"(and any special sessions) before 1 January.")
        return None
