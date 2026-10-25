"""Minimal pcap reader that yields decoded MetaRoCE packets.

Reads classic libpcap files (what `tcpdump -w` writes), microsecond or
nanosecond timestamps, either byte order. Link types: Ethernet, raw IP,
Linux cooked v1/v2. No third-party dependencies.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass
from typing import BinaryIO

from .headers import UDP_PORT, DecodeError, Packet, decode

LINKTYPE_ETHERNET = 1
LINKTYPE_RAW = 101
LINKTYPE_LINUX_SLL = 113
LINKTYPE_LINUX_SLL2 = 276

_IPV6_EXT = {0, 43, 60}  # hop-by-hop, routing, destination options
_IPV6_FRAG = 44


@dataclass
class Frame:
    ts_ns: int
    src: str
    dst: str
    sport: int
    dport: int
    ecn: int  # IP ECN codepoint: 0 not-ECT, 1/2 ECT, 3 CE
    hop_limit: int
    packet: Packet | None
    error: str | None = None
    udp_len: int = 0  # UDP payload bytes on the wire, even if the capture was truncated


def _ip6(b: bytes) -> str:
    import ipaddress

    return str(ipaddress.IPv6Address(b))


def _ip4(b: bytes) -> str:
    return ".".join(str(x) for x in b)


def read_frames(f: BinaryIO, *, port: int = UDP_PORT, has_mcrc: bool = True) -> Iterator[Frame]:
    """Yield a Frame for every UDP packet to or from `port`."""
    hdr = f.read(24)
    if len(hdr) < 24:
        return
    magic = hdr[:4]
    if magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"):
        endian = "<"
    elif magic in (b"\xa1\xb2\xc3\xd4", b"\xa1\xb2\x3c\x4d"):
        endian = ">"
    else:
        raise ValueError("not a classic pcap file (pcapng is not supported; use tcpdump -w)")
    nanos = magic in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d")
    linktype = struct.unpack(endian + "I", hdr[20:24])[0] & 0x0FFFFFFF
    rec = struct.Struct(endian + "IIII")
    while True:
        rh = f.read(16)
        if len(rh) < 16:
            return
        sec, frac, caplen, _ = rec.unpack(rh)
        data = f.read(caplen)
        ts_ns = sec * 1_000_000_000 + (frac if nanos else frac * 1000)
        fr = _parse(data, linktype, ts_ns, port, has_mcrc)
        if fr is not None:
            yield fr


def _parse(d: bytes, linktype: int, ts_ns: int, port: int, has_mcrc: bool) -> Frame | None:
    if linktype == LINKTYPE_ETHERNET:
        off, etype = 14, int.from_bytes(d[12:14], "big")
        while etype in (0x8100, 0x88A8) and len(d) >= off + 4:
            etype = int.from_bytes(d[off + 2:off + 4], "big")
            off += 4
    elif linktype == LINKTYPE_LINUX_SLL:
        off, etype = 16, int.from_bytes(d[14:16], "big")
    elif linktype == LINKTYPE_LINUX_SLL2:
        off, etype = 20, int.from_bytes(d[0:2], "big")
    elif linktype == LINKTYPE_RAW:
        off = 0
        etype = 0x86DD if d and d[0] >> 4 == 6 else 0x0800
    else:
        raise ValueError(f"unsupported pcap link type {linktype}")

    if etype == 0x86DD:
        if len(d) < off + 40:
            return None
        tc = (int.from_bytes(d[off:off + 4], "big") >> 20) & 0xFF
        nxt, hlim = d[off + 6], d[off + 7]
        src, dst = _ip6(d[off + 8:off + 24]), _ip6(d[off + 24:off + 40])
        off += 40
        while nxt in _IPV6_EXT or nxt == _IPV6_FRAG:
            if len(d) < off + 8:
                return None
            if nxt == _IPV6_FRAG:
                nxt, off = d[off], off + 8
            else:
                nxt, off = d[off], off + (d[off + 1] + 1) * 8
        ecn = tc & 0x3
    elif etype == 0x0800:
        if len(d) < off + 20:
            return None
        ihl = (d[off] & 0xF) * 4
        ecn, hlim, nxt = d[off + 1] & 0x3, d[off + 8], d[off + 9]
        src, dst = _ip4(d[off + 12:off + 16]), _ip4(d[off + 16:off + 20])
        off += ihl
    else:
        return None
    if nxt != 17 or len(d) < off + 8:
        return None
    sport, dport, ulen = struct.unpack_from("!HHH", d, off)
    if port not in (sport, dport):
        return None
    payload = d[off + 8:off + max(ulen, 8)]
    truncated = len(payload) < ulen - 8
    try:
        # A truncated capture has no trailing mCRC to check.
        pkt = decode(payload, has_mcrc=has_mcrc and not truncated)
        err = None
    except DecodeError as e:
        pkt, err = None, str(e)
    return Frame(ts_ns, src, dst, sport, dport, ecn, hlim, pkt, err, udp_len=ulen - 8)
