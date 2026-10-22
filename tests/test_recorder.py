"""Hourly bars from live ticks: bucketing by trade time, LTT calibration,
locking of checked bars, official-open precedence, and the feed parser."""
from __future__ import annotations

import struct
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config
from backend.feed import parse_frame
from backend.recorder import IST_SHIFT, HourlyRecorder

D = "2026-11-02"


def ep(hms: str) -> float:
    h, m, s = map(int, hms.split(":"))
    return datetime(2026, 11, 2, h, m, s, tzinfo=config.IST).timestamp()


def test_trades_bucket_into_hour_bars_labelled_at_their_open():
    r = HourlyRecorder()
    r.reset(D)
    for t, px in (("09:15:01", 100), ("09:40:00", 104), ("10:14:59", 101),
                  ("10:15:00", 102), ("15:29:59", 99), ("15:30:00", 1)):
        r.on_trade("AAA", px, None, None, now=ep(t))
    b = r.bars["AAA"]
    assert b["09:15"] == {"o": 100, "h": 104, "l": 100, "c": 101, "n": 3}
    assert b["10:15"]["o"] == 102
    assert b["15:15"]["c"] == 99                    # 15:30 print excluded
    assert set(b) == {"09:15", "10:15", "15:15"}


def test_ltt_calibrates_to_ist_shifted_epoch_and_buckets_by_trade_time():
    r = HourlyRecorder()
    r.reset(D)
    # 20 fresh ticks whose LTT is IST-shifted epoch
    for i in range(20):
        now = ep("09:20:00") + i
        r.on_trade("AAA", 100 + i, int(now) + IST_SHIFT, None, now=now)
    assert r.ltt_mode == "ist"
    # a trade at 10:14:58 that reaches us at 10:15:03 stays in the 09:15 bar
    r.on_trade("AAA", 77, int(ep("10:14:58")) + IST_SHIFT, None,
               now=ep("10:15:03"))
    assert r.bars["AAA"]["09:15"]["c"] == 77
    assert "10:15" not in r.bars["AAA"]


def test_ltt_calibrates_to_utc_epoch_too():
    r = HourlyRecorder()
    r.reset(D)
    for i in range(20):
        now = ep("09:20:00") + i
        r.on_trade("AAA", 100, int(now), None, now=now)
    assert r.ltt_mode == "utc"


def test_locked_bar_is_never_rewritten_by_a_straggler():
    r = HourlyRecorder()
    r.reset(D)
    r.on_trade("AAA", 100, None, None, now=ep("09:30:00"))
    r.lock_through(1)
    r.on_trade("AAA", 1, None, None, now=ep("09:59:00"))   # late, bar 0 locked
    assert r.bars["AAA"]["09:15"]["c"] == 100 and r.late_dropped == 1


def test_official_open_rest_beats_feed_and_preopen_is_ignored():
    r = HourlyRecorder()
    r.reset(D)
    r.on_trade("AAA", 100, None, 98.0, now=ep("09:05:00"))  # pre-open: ignored
    assert "AAA" not in r.opens
    r.on_trade("AAA", 100, None, 99.0, now=ep("09:15:02"))
    assert r.opens["AAA"] == (99.0, "feed")
    r.set_official_open("AAA", 99.5, "rest")
    r.on_trade("BBB", 10, None, 9.0, now=ep("09:15:03"))
    r.set_official_open("AAA", 1.0, "feed")                 # can't override REST
    assert r.opens["AAA"] == (99.5, "rest")


def test_old_trade_snapshot_from_yesterday_is_ignored():
    r = HourlyRecorder()
    r.reset(D)
    for i in range(20):
        now = ep("09:20:00") + i
        r.on_trade("AAA", 100, int(now), None, now=now)
    yday = datetime(2026, 10, 30, 15, 29, 0, tzinfo=config.IST).timestamp()
    r.on_trade("BBB", 55, int(yday), None, now=ep("10:00:00"))
    assert "BBB" not in r.bars


def test_feed_parser_reads_a_quote_packet():
    body = struct.pack("<fhifiiiffff", 101.5, 3, 1_700_000_000, 101.2, 5000,
                       10, 20, 100.0, 99.0, 102.0, 98.5)
    hdr = struct.pack("<BHBI", config.RESP_QUOTE, 8 + len(body), 1, 2885)
    ticks = parse_frame(hdr + body)
    assert len(ticks) == 1
    t = ticks[0]
    assert t.kind == "quote" and t.security_id == 2885 and t.segment == "NSE_EQ"
    assert abs(t.ltp - 101.5) < 1e-4 and t.open == 100.0
    assert t.last_trade_time == 1_700_000_000


def test_uncalibrated_stale_snapshot_never_invents_a_print():
    r = HourlyRecorder()
    r.reset(D)
    stale = int(ep("10:02:00"))                     # halted since 10:02
    r.on_trade("HALT", 55, stale, None, now=ep("11:30:00"))
    assert "HALT" not in r.bars
