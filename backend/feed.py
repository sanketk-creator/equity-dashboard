"""
Dhan live market feed client.

Dhan pushes market data as raw binary over a websocket -- not JSON. Every
message begins with an 8-byte header telling us what kind of packet it is, how
long it is, and which instrument it belongs to. Several packets can be glued
together inside a single websocket frame, so we walk the buffer.

Byte layouts below come straight from the DhanHQ v2 "Live Market Feed" spec.
Everything is little-endian.
"""
from __future__ import annotations

import asyncio
import json
import logging
import struct
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable, Sequence

import websockets

from . import config

log = logging.getLogger("feed")

# 8-byte header: code(u8) length(u16) segment(u8) security_id(u32)
_HEADER = struct.Struct("<BHBI")
_HEADER_SIZE = _HEADER.size  # 8

# Payload layouts (everything AFTER the 8-byte header)
_TICKER = struct.Struct("<fi")                      # ltp, last_trade_time
_PREV_CLOSE = struct.Struct("<fi")                  # prev_close, prev_oi
_OI = struct.Struct("<i")                           # open_interest
_QUOTE = struct.Struct("<fhifiiiffff")              # 42 bytes
_FULL = struct.Struct("<fhifiiiiiiffff")            # 54 bytes, then 100B depth
_DEPTH_LEVEL = struct.Struct("<iihhff")             # 20 bytes x 5
_DISCONNECT = struct.Struct("<h")


@dataclass
class Tick:
    """One normalised market update. Not every field is present in every packet."""

    security_id: int
    segment: str
    kind: str                       # ticker | quote | full | oi | prev_close
    ltp: float | None = None
    last_trade_time: int | None = None
    last_traded_qty: int | None = None
    avg_price: float | None = None
    volume: int | None = None
    total_buy_qty: int | None = None
    total_sell_qty: int | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    prev_close: float | None = None
    open_interest: int | None = None
    prev_open_interest: int | None = None
    bid: float | None = None
    ask: float | None = None
    depth: list[dict] | None = None


def _segment_name(code: int) -> str:
    return config.SEGMENT_NAME_BY_CODE.get(code, f"SEG_{code}")


def parse_frame(buf: bytes) -> list[Tick]:
    """Split a websocket frame into however many packets it contains."""
    ticks: list[Tick] = []
    offset = 0
    total = len(buf)

    while offset + _HEADER_SIZE <= total:
        code, msg_len, seg_code, sec_id = _HEADER.unpack_from(buf, offset)

        # A declared length shorter than the header itself means the frame is
        # corrupt. Advancing by it would mis-frame everything that follows and
        # silently drop the rest of the batch, so stop here instead.
        if msg_len < _HEADER_SIZE:
            log.debug("corrupt packet length %s (code %s); dropping rest of frame", msg_len, code)
            break
        if offset + msg_len > total:
            msg_len = total - offset

        body = offset + _HEADER_SIZE
        packet_end = offset + msg_len
        segment = _segment_name(seg_code)

        try:
            tick = _parse_packet(code, buf, body, segment, sec_id, packet_end)
        except struct.error:
            log.debug("short packet code=%s len=%s", code, msg_len)
            tick = None

        if tick is not None:
            ticks.append(tick)

        offset += msg_len

    return ticks


def _parse_packet(code: int, buf: bytes, body: int, segment: str, sec_id: int,
                  packet_end: int) -> Tick | None:
    if code == config.RESP_TICKER:
        ltp, ltt = _TICKER.unpack_from(buf, body)
        return Tick(sec_id, segment, "ticker", ltp=ltp, last_trade_time=ltt)

    if code == config.RESP_PREV_CLOSE:
        prev_close, prev_oi = _PREV_CLOSE.unpack_from(buf, body)
        return Tick(sec_id, segment, "prev_close", prev_close=prev_close, prev_open_interest=prev_oi)

    if code == config.RESP_OI:
        (oi,) = _OI.unpack_from(buf, body)
        return Tick(sec_id, segment, "oi", open_interest=oi)

    if code == config.RESP_QUOTE:
        (ltp, ltq, ltt, atp, vol, sell_q, buy_q,
         d_open, d_close, d_high, d_low) = _QUOTE.unpack_from(buf, body)
        return Tick(
            sec_id, segment, "quote", ltp=ltp, last_traded_qty=ltq, last_trade_time=ltt,
            avg_price=atp, volume=vol, total_sell_qty=sell_q, total_buy_qty=buy_q,
            open=d_open, close=d_close, high=d_high, low=d_low,
        )

    if code == config.RESP_FULL:
        (ltp, ltq, ltt, atp, vol, sell_q, buy_q, oi, _oi_hi, _oi_lo,
         d_open, d_close, d_high, d_low) = _FULL.unpack_from(buf, body)
        depth: list[dict] = []
        dstart = body + _FULL.size
        for lvl in range(5):
            off = dstart + lvl * _DEPTH_LEVEL.size
            # Bound against THIS packet's end, not the whole frame -- otherwise
            # a truncated Full packet reads the next packet's bytes as depth.
            if off + _DEPTH_LEVEL.size > packet_end:
                break
            bq, aq, bo, ao, bp, ap = _DEPTH_LEVEL.unpack_from(buf, off)
            depth.append({"bid_qty": bq, "ask_qty": aq, "bid_orders": bo,
                          "ask_orders": ao, "bid": round(bp, 2), "ask": round(ap, 2)})
        return Tick(
            sec_id, segment, "full", ltp=ltp, last_traded_qty=ltq, last_trade_time=ltt,
            avg_price=atp, volume=vol, total_sell_qty=sell_q, total_buy_qty=buy_q,
            open=d_open, close=d_close, high=d_high, low=d_low, open_interest=oi,
            bid=depth[0]["bid"] if depth else None,
            ask=depth[0]["ask"] if depth else None,
            depth=depth or None,
        )

    if code == config.RESP_DISCONNECT:
        (reason,) = _DISCONNECT.unpack_from(buf, body)
        log.warning("Dhan sent a disconnect packet, reason code %s", reason)
        return None

    return None


@dataclass(frozen=True)
class Instrument:
    segment: str
    security_id: int

    def as_payload(self) -> dict[str, str]:
        return {"ExchangeSegment": self.segment, "SecurityId": str(self.security_id)}


class DhanFeed:
    """
    Keeps one websocket alive, re-subscribing after every reconnect.

    Dhan allows 5 concurrent connections, 5000 instruments each, and at most 100
    instruments per subscribe message -- so we chunk.
    """

    def __init__(self, on_tick: Callable[[Tick], Awaitable[None] | None]):
        self._on_tick = on_tick
        self._instruments: dict[Instrument, int] = {}   # instrument -> request code
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._lock = asyncio.Lock()
        self._pending: list[tuple[Instrument, int]] = []
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

        self.connected = False
        self.last_tick_at: float | None = None
        self.reconnects = 0
        self.last_error: str | None = None

    # -- public API ---------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="dhan-feed")

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            await self._ws.close()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def reconnect(self) -> None:
        """
        Drop the socket so the run loop redials -- used after a token change.

        Subscriptions are kept, because `_run` re-sends the whole instrument
        list on every connect.
        """
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        elif (self._task is None or self._task.done()) and not self._stop.is_set():
            self.start()

    async def subscribe(self, instruments: Iterable[Instrument], request_code: int) -> None:
        """Add instruments to the feed. Safe to call before the socket is up."""
        fresh = [
            (ins, request_code)
            for ins in instruments
            if self._instruments.get(ins) != request_code
        ]
        if not fresh:
            return
        for ins, code in fresh:
            self._instruments[ins] = code
        async with self._lock:
            if self._ws is not None and self.connected:
                await self._send_subscriptions(fresh)
            else:
                self._pending.extend(fresh)

    def tracked_count(self) -> int:
        return len(self._instruments)

    # -- internals ----------------------------------------------------------

    async def _send_subscriptions(self, pairs: Sequence[tuple[Instrument, int]]) -> None:
        by_code: dict[int, list[Instrument]] = {}
        for ins, code in pairs:
            by_code.setdefault(code, []).append(ins)

        for code, group in by_code.items():
            for i in range(0, len(group), config.MAX_INSTRUMENTS_PER_MESSAGE):
                chunk = group[i:i + config.MAX_INSTRUMENTS_PER_MESSAGE]
                msg = {
                    "RequestCode": code,
                    "InstrumentCount": len(chunk),
                    "InstrumentList": [c.as_payload() for c in chunk],
                }
                assert self._ws is not None
                await self._ws.send(json.dumps(msg))
                await asyncio.sleep(0.05)   # be gentle

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    config.feed_url(),
                    ping_interval=None,      # Dhan drives the keepalive itself
                    close_timeout=5,
                    max_size=None,
                ) as ws:
                    self._ws = ws
                    self.connected = True
                    self.last_error = None
                    backoff = 1.0
                    log.info("Dhan feed connected (%d instruments)", len(self._instruments))

                    async with self._lock:
                        everything = list(self._instruments.items())
                        self._pending.clear()
                    if everything:
                        await self._send_subscriptions(everything)

                    async for raw in ws:
                        if isinstance(raw, str):
                            continue
                        self.last_tick_at = asyncio.get_running_loop().time()
                        for tick in parse_frame(raw):
                            result = self._on_tick(tick)
                            if asyncio.iscoroutine(result):
                                await result

            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - we genuinely want to retry on anything
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("Dhan feed dropped (%s); retrying in %.0fs", self.last_error, backoff)
            finally:
                self.connected = False
                self._ws = None

            if self._stop.is_set():
                break
            self.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
