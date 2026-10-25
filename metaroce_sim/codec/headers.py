"""MetaRoCE packet headers (spec 1.0, chapter 8) and the mCRC (§4.1).

`decode()` turns the UDP payload of a MetaRoCE packet into a `Packet`;
`encode()` turns a `Packet` back into bytes. Reserved fields, padding
bytes and abbreviated ACK bit vectors are kept as received, so
`encode(decode(b)) == b` holds for any well-formed packet.

Layouts follow the spec figures and were cross-checked against
libsoftmetaroce's codec (`libsoftmetaroce/codec/*`). Where the spec's
atomic figures omit the data header, the opcode table and LSMR agree
that atomics carry METH, and this module does too.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from enum import IntEnum

UDP_PORT = 2766
"""Default UDP destination port identifying MetaRoCE (§4.1)."""

BTH_META_ROCE = 0b110
"""Opcode bits [7:5] for MetaRoCE connected mode (§8.1.1)."""

MCRC_RESIDUE = 0x2144DF1C
"""CRC-32 of (data + its little-endian CRC); LSMR's `kExpectedIcrcValue`."""

BASE_LEN = 12
METH_LEN = 20
RETH_LEN = 16
IMM_LEN = 4
IETH_LEN = 4
ATOMIC_LEN = 28
ATOMIC_ACK_LEN = 8
ACK_LEN = 20
RNR_CANCEL_LEN = 8
CONTROL_LEN = 8
PING_LEN = 8
BITVEC_LEN = 32


class Op(IntEnum):
    """Operation type, opcode bits [4:0] (Table 8.1)."""

    SEND = 0b00000
    SEND_IMM_P = 0b00001
    WRITE_IMM_P = 0b00010
    SEND_INV = 0b00011
    SEND_LAST = 0b00100
    WRITE = 0b00110
    WRITE_IMM = 0b00111
    READ = 0b01100
    READ_IMM_P = 0b01101
    READ_RESP = 0b01111
    RNR_CANCEL = 0b10000
    SEL_ACK = 0b10001
    ATOMIC_ACK = 0b10010
    FETCH_ADD = 0b10100
    CMP_SWAP = 0b10101
    RELIABLE_CONTROL = 0b11110
    UNRELIABLE_CONTROL = 0b11111


RETH_OPS = {Op.WRITE, Op.WRITE_IMM, Op.WRITE_IMM_P, Op.READ, Op.READ_IMM_P}
IMM_OPS = {Op.SEND_IMM_P, Op.WRITE_IMM_P, Op.READ_IMM_P}
ATOMIC_OPS = {Op.FETCH_ADD, Op.CMP_SWAP}
NO_PAYLOAD_OPS = {Op.READ, Op.READ_IMM_P, Op.FETCH_ADD, Op.CMP_SWAP, Op.ATOMIC_ACK}


def opcode(op: Op) -> int:
    """Full 8-bit opcode for a MetaRoCE operation type."""
    return (BTH_META_ROCE << 5) | int(op)


class DecodeError(ValueError):
    """The bytes are not a well-formed MetaRoCE packet."""


# --------------------------------------------------------------------- mCRC


def mcrc(data: bytes) -> int:
    """mCRC over headers, payload and padding: standard CRC-32 (§4.1)."""
    return zlib.crc32(data) & 0xFFFFFFFF


def mcrc_ok(data_with_crc: bytes) -> bool:
    """True if the trailing 4 bytes are a valid mCRC for what precedes them."""
    return len(data_with_crc) >= 4 and zlib.crc32(data_with_crc) == MCRC_RESIDUE


# ------------------------------------------------------------------ headers


@dataclass
class BaseHeader:
    opcode: int
    flow_id: int
    dst_qp: int
    src_qp: int
    version: int = 0
    reserved: int = 0

    @property
    def op(self) -> Op | None:
        try:
            return Op(self.opcode & 0x1F)
        except ValueError:
            return None

    @property
    def is_meta_roce(self) -> bool:
        return (self.opcode >> 5) == BTH_META_ROCE

    def pack(self) -> bytes:
        w0 = (self.version & 0xF) << 12 | (self.flow_id & 0xFFF)
        return struct.pack("!BBHII", self.opcode, self.reserved, w0, self.dst_qp, self.src_qp)

    @classmethod
    def unpack(cls, b: bytes, off: int = 0) -> BaseHeader:
        opc, rsv, w0, dqp, sqp = struct.unpack_from("!BBHII", b, off)
        return cls(opc, w0 & 0xFFF, dqp, sqp, version=w0 >> 12, reserved=rsv)


@dataclass
class DataHeader:
    """METH (§8.2)."""

    fsn: int
    una: int
    posn: int
    msn: int
    csn: int = 0
    tag: int = 0
    ack_req: bool = False
    retx: bool = False
    timeout: bool = False
    pad: int = 0
    rate_hint: int = 0
    tx_ts: int = 0
    reserved: int = 0

    def pack(self) -> bytes:
        flags = (
            self.ack_req << 7 | self.retx << 6 | self.timeout << 5
            | (self.reserved & 1) << 4 | (self.pad & 0xF)
        )
        return struct.pack(
            "!HHBBHHHII", self.fsn, self.una, self.tag, flags, self.posn,
            self.csn, self.msn, self.rate_hint, self.tx_ts,
        )

    @classmethod
    def unpack(cls, b: bytes, off: int) -> DataHeader:
        fsn, una, tag, fl, posn, csn, msn, rate, ts = struct.unpack_from("!HHBBHHHII", b, off)
        return cls(
            fsn=fsn, una=una, posn=posn, msn=msn, csn=csn, tag=tag,
            ack_req=bool(fl & 0x80), retx=bool(fl & 0x40), timeout=bool(fl & 0x20),
            reserved=(fl >> 4) & 1, pad=fl & 0xF, rate_hint=rate, tx_ts=ts,
        )


@dataclass
class Reth:
    """Read/Write header (§8.3)."""

    addr: int
    rkey: int
    length: int

    def pack(self) -> bytes:
        return struct.pack("!QII", self.addr, self.rkey, self.length)

    @classmethod
    def unpack(cls, b: bytes, off: int) -> Reth:
        return cls(*struct.unpack_from("!QII", b, off))


@dataclass
class AtomicEth:
    """AtomicETH (§8.8.1)."""

    va: int
    rkey: int
    swap_add: int
    compare: int

    def pack(self) -> bytes:
        return struct.pack("!QIQQ", self.va, self.rkey, self.swap_add, self.compare)

    @classmethod
    def unpack(cls, b: bytes, off: int) -> AtomicEth:
        return cls(*struct.unpack_from("!QIQQ", b, off))


class AckKind(IntEnum):
    ACK = 0
    SACK = 1
    HRNR = 2
    BRNR = 3
    NAK = 4


NAK_REASONS = {
    0: "flow-id-unsupported",
    1: "invalid-request",
    2: "access-error",
    3: "operational-error",
}


@dataclass
class AckHeader:
    """SAETH (§8.5). Bit vectors are kept as raw bytes, possibly shortened."""

    cum_fsn: int
    max_fsn: int
    status: int = 0
    cnp: int = 0
    tag: int = 0
    cdmsn: int = 0
    active_ports: int = 0
    rate_hint: int = 0
    echo_ts: int = 0
    fsn_vec: bytes = b""
    rnr_vec: bytes = b""
    rsv0: int = 0
    rsv1: int = 0
    rsv2: int = 0

    @property
    def kind(self) -> AckKind:
        hi = self.status >> 5
        if hi == 0b000:
            return AckKind.SACK if self.status & 1 else AckKind.ACK
        if hi == 0b001:
            return AckKind.HRNR
        if hi == 0b010:
            return AckKind.BRNR
        return AckKind.NAK

    @property
    def rnr_backoff_us(self) -> int | None:
        """Requested RNR backoff in microseconds (Table 8.4), or None."""
        if self.kind not in (AckKind.HRNR, AckKind.BRNR):
            return None
        n = self.status & 0x1F
        return 0 if n == 0 else 5 << n

    def pack(self) -> bytes:
        return struct.pack(
            "!BBHHBBHHHHI",
            (self.rsv0 & 0xF) << 4 | (self.cnp & 0xF), self.tag, self.cum_fsn,
            self.max_fsn, self.status, self.rsv1,
            self.cdmsn, self.active_ports, self.rsv2, self.rate_hint, self.echo_ts,
        ) + self.fsn_vec + self.rnr_vec

    @classmethod
    def unpack(cls, b: bytes, off: int, end: int) -> AckHeader:
        if end - off < ACK_LEN:
            raise DecodeError("ack shorter than SAETH")
        (c, tag, cum, mx, st, r1, cdmsn, ports, r2, rate, ets) = struct.unpack_from(
            "!BBHHBBHHHHI", b, off
        )
        rest = bytes(b[off + ACK_LEN:end])
        if (st >> 5) == 0b010:  # BRNR: full FSN vector, then the RNR vector
            fsn_vec, rnr_vec = rest[:BITVEC_LEN], rest[BITVEC_LEN:]
        else:
            fsn_vec, rnr_vec = rest, b""
        return cls(
            cum_fsn=cum, max_fsn=mx, status=st, cnp=c & 0xF, tag=tag, cdmsn=cdmsn,
            active_ports=ports, rate_hint=rate, echo_ts=ets, fsn_vec=fsn_vec,
            rnr_vec=rnr_vec, rsv0=c >> 4, rsv1=r1, rsv2=r2,
        )

    def sacked_fsns(self) -> list[int]:
        """FSNs selectively acknowledged beyond `cum_fsn` (§8.5.9), with 16-bit wrap."""
        return _vec_fsns(self.fsn_vec, self.cum_fsn)

    def rnr_fsns(self) -> list[int]:
        """FSNs received but refused (BRNR bit vector, §8.5.10)."""
        return _vec_fsns(self.rnr_vec, self.cum_fsn)


def _vec_fsns(vec: bytes, cum_fsn: int) -> list[int]:
    out = []
    for w in range(min(len(vec), BITVEC_LEN) // 4):
        word = int.from_bytes(vec[4 * w:4 * w + 4], "big")
        while word:
            low = word & -word
            bit = low.bit_length() - 1
            out.append((cum_fsn + 1 + 32 * w + bit) & 0xFFFF)
            word ^= low
    return out


def make_fsn_vec(cum_fsn: int, fsns) -> bytes:
    """Build a 32-byte FSN bit vector from FSNs after `cum_fsn`."""
    words = [0] * 8
    for f in fsns:
        off = (f - cum_fsn - 1) & 0xFFFF
        if off < 256:
            words[off // 32] |= 1 << (off % 32)
    return b"".join(w.to_bytes(4, "big") for w in words)


@dataclass
class RnrCancel:
    """RNR-Cancel / Receiver Is Ready (§8.6)."""

    flows_avail: int
    tag: int
    posted_csn: int
    rate_hint: int = 0
    reserved: int = 0

    def pack(self) -> bytes:
        return struct.pack(
            "!BBHHH", self.flows_avail, self.tag, self.posted_csn, self.reserved, self.rate_hint
        )

    @classmethod
    def unpack(cls, b: bytes, off: int) -> RnrCancel:
        fa, tag, csn, rsv, rate = struct.unpack_from("!BBHHH", b, off)
        return cls(fa, tag, csn, rate, rsv)


@dataclass
class ControlHeader:
    """Control header (§8.7): type, response/echo id, timestamp."""

    type: int
    resp_id: int
    ts: int

    def pack(self) -> bytes:
        return struct.pack("!HHI", self.type, self.resp_id, self.ts)

    @classmethod
    def unpack(cls, b: bytes, off: int) -> ControlHeader:
        return cls(*struct.unpack_from("!HHI", b, off))


@dataclass
class PingHeader:
    """Echo request/response fields after the control header (§8.7.1)."""

    tag: int
    error_state: int
    flow_count: int
    active_ports: int
    reserved: int = 0

    def pack(self) -> bytes:
        return struct.pack(
            "!BBHHH", self.tag, self.error_state, self.flow_count, self.reserved, self.active_ports
        )

    @classmethod
    def unpack(cls, b: bytes, off: int) -> PingHeader:
        tag, err, fc, rsv, ports = struct.unpack_from("!BBHHH", b, off)
        return cls(tag, err, fc, ports, rsv)


PING_TYPES = {0, 1}


# ------------------------------------------------------------------- packet


@dataclass
class Packet:
    base: BaseHeader
    data: DataHeader | None = None
    reth: Reth | None = None
    imm: int | None = None
    ieth_rkey: int | None = None
    atomic: AtomicEth | None = None
    atomic_orig: int | None = None
    ack: AckHeader | None = None
    rnr_cancel: RnrCancel | None = None
    control: ControlHeader | None = None
    ping: PingHeader | None = None
    payload: bytes = b""
    padding: bytes = b""
    mcrc: int | None = None
    mcrc_valid: bool | None = field(default=None, compare=False)

    @property
    def op(self) -> Op | None:
        return self.base.op


def decode(buf: bytes, *, has_mcrc: bool = True) -> Packet:
    """Decode a MetaRoCE UDP payload. Raises DecodeError on malformed input."""
    b = bytes(buf)
    end = len(b)
    crc = None
    crc_valid = None
    if has_mcrc:
        if end < BASE_LEN + 4:
            raise DecodeError("too short for base header + mCRC")
        crc_valid = mcrc_ok(b)
        crc = int.from_bytes(b[-4:], "little")
        end -= 4
    if end < BASE_LEN:
        raise DecodeError("too short for base header")
    base = BaseHeader.unpack(b, 0)
    if not base.is_meta_roce:
        raise DecodeError(f"opcode 0x{base.opcode:02x} is not MetaRoCE")
    op = base.op
    if op is None:
        raise DecodeError(f"reserved opcode 0x{base.opcode:02x}")
    pkt = Packet(base=base, mcrc=crc, mcrc_valid=crc_valid)
    off = BASE_LEN

    def need(n: int) -> None:
        if end - off < n:
            raise DecodeError(f"{op.name}: need {n} bytes at offset {off}, have {end - off}")

    if op == Op.SEL_ACK:
        pkt.ack = AckHeader.unpack(b, off, end)
        return pkt
    if op == Op.RNR_CANCEL:
        need(RNR_CANCEL_LEN)
        pkt.rnr_cancel = RnrCancel.unpack(b, off)
        off += RNR_CANCEL_LEN
        pkt.payload = b[off:end]
        return pkt
    if op == Op.UNRELIABLE_CONTROL:
        return _decode_control(pkt, b, off, end)
    # Everything else carries METH (reliable control included).
    need(METH_LEN)
    pkt.data = DataHeader.unpack(b, off)
    off += METH_LEN
    if op == Op.RELIABLE_CONTROL:
        return _decode_control(pkt, b, off, end)
    if op in RETH_OPS:
        need(RETH_LEN)
        pkt.reth = Reth.unpack(b, off)
        off += RETH_LEN
    if op in IMM_OPS:
        need(IMM_LEN)
        pkt.imm = int.from_bytes(b[off:off + 4], "big")
        off += IMM_LEN
    if op == Op.SEND_INV:
        need(IETH_LEN)
        pkt.ieth_rkey = int.from_bytes(b[off:off + 4], "big")
        off += IETH_LEN
    if op in ATOMIC_OPS:
        need(ATOMIC_LEN)
        pkt.atomic = AtomicEth.unpack(b, off)
        off += ATOMIC_LEN
    if op == Op.ATOMIC_ACK:
        need(ATOMIC_ACK_LEN)
        pkt.atomic_orig = int.from_bytes(b[off:off + 8], "big")
        off += ATOMIC_ACK_LEN
    pad = pkt.data.pad
    if end - off < pad:
        raise DecodeError("padding longer than payload")
    pkt.payload = b[off:end - pad]
    pkt.padding = b[end - pad:end]
    return pkt


def _decode_control(pkt: Packet, b: bytes, off: int, end: int) -> Packet:
    if end - off < CONTROL_LEN:
        raise DecodeError("control packet shorter than control header")
    pkt.control = ControlHeader.unpack(b, off)
    off += CONTROL_LEN
    if pkt.control.type in PING_TYPES and end - off >= PING_LEN:
        pkt.ping = PingHeader.unpack(b, off)
        off += PING_LEN
    pkt.payload = b[off:end]
    return pkt


def encode(pkt: Packet, *, with_mcrc: bool = True) -> bytes:
    """Serialize a Packet. Computes a fresh mCRC when `with_mcrc`."""
    parts = [pkt.base.pack()]
    if pkt.ack is not None:
        parts.append(pkt.ack.pack())
    elif pkt.rnr_cancel is not None:
        parts += [pkt.rnr_cancel.pack(), pkt.payload]
    else:
        if pkt.data is not None:
            parts.append(pkt.data.pack())
        if pkt.control is not None:
            parts.append(pkt.control.pack())
            if pkt.ping is not None:
                parts.append(pkt.ping.pack())
        if pkt.reth is not None:
            parts.append(pkt.reth.pack())
        if pkt.imm is not None:
            parts.append(pkt.imm.to_bytes(4, "big"))
        if pkt.ieth_rkey is not None:
            parts.append(pkt.ieth_rkey.to_bytes(4, "big"))
        if pkt.atomic is not None:
            parts.append(pkt.atomic.pack())
        if pkt.atomic_orig is not None:
            parts.append(pkt.atomic_orig.to_bytes(8, "big"))
        parts += [pkt.payload, pkt.padding]
    body = b"".join(parts)
    if with_mcrc:
        body += mcrc(body).to_bytes(4, "little")
    return body
