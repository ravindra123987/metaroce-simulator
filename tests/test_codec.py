import io
import struct

import pytest

from metaroce_sim.codec import (
    AckHeader,
    AckKind,
    AtomicEth,
    BaseHeader,
    ControlHeader,
    DataHeader,
    DecodeError,
    Op,
    Packet,
    PingHeader,
    Reth,
    RnrCancel,
    decode,
    encode,
    make_fsn_vec,
    mcrc,
    mcrc_ok,
    opcode,
)
from metaroce_sim.codec.pcap import read_frames
from metaroce_sim.codec.summary import summarize, to_dict


def base(op, flow=3, dqp=0x1234, sqp=0x5678):
    return BaseHeader(opcode=opcode(op), flow_id=flow, dst_qp=dqp, src_qp=sqp)


def meth(**kw):
    d = {"fsn": 10, "una": 7, "posn": 2, "msn": 5, "csn": 1, "tag": 4,
         "rate_hint": 400, "tx_ts": 0xDEADBEEF}
    d.update(kw)
    return DataHeader(**d)


# ------------------------------------------------------------------- mCRC


def test_mcrc_is_standard_crc32():
    assert mcrc(b"123456789") == 0xCBF43926


def test_mcrc_residue_check():
    body = b"metaroce"
    framed = body + mcrc(body).to_bytes(4, "little")
    assert mcrc_ok(framed)
    assert not mcrc_ok(framed[:-1] + bytes([framed[-1] ^ 1]))


# ----------------------------------------------------------- base and METH


def test_opcode_transport_bits():
    assert opcode(Op.SEL_ACK) == 0xD1  # Figure 8.5 "Code: 0xd1"
    assert opcode(Op.RNR_CANCEL) == 0xD0
    assert opcode(Op.ATOMIC_ACK) == 0xD2
    assert opcode(Op.UNRELIABLE_CONTROL) == 0xDF
    assert opcode(Op.SEND_INV) == 0xC3


def test_base_header_layout():
    raw = base(Op.SEND, flow=0xABC).pack()
    assert len(raw) == 12
    assert raw[0] == 0xC0 and raw[2:4] == b"\x0a\xbc"  # version 0, 12-bit flow ID
    assert BaseHeader.unpack(raw).flow_id == 0xABC


def test_meth_flag_bits_and_size():
    h = meth(ack_req=True, retx=False, timeout=True, pad=5)
    raw = h.pack()
    assert len(raw) == 20  # base + METH = 32 bytes (spec §4.1)
    assert raw[4] == 4  # tag
    assert raw[5] == 0x80 | 0x20 | 5
    assert DataHeader.unpack(raw, 0) == h


@pytest.mark.parametrize(
    "op,extra",
    [
        (Op.SEND, {}),
        (Op.SEND_LAST, {}),
        (Op.SEND_IMM_P, {"imm": 0x01020304}),
        (Op.SEND_INV, {"ieth_rkey": 0xCAFE}),
        (Op.WRITE, {"reth": Reth(0x1000_0000_0000, 0x77, 65536)}),
        (Op.WRITE_IMM, {"reth": Reth(0x2000, 0x77, 8192)}),
        (Op.WRITE_IMM_P, {"reth": Reth(0x2000, 0x77, 8192), "imm": 9}),
        (Op.READ_RESP, {}),
    ],
)
def test_data_packets_round_trip(op, extra):
    pkt = Packet(base=base(op), data=meth(pad=3), payload=b"x" * 61, padding=b"\0\0\0", **extra)
    raw = encode(pkt)
    got = decode(raw)
    assert got.mcrc_valid
    assert got.op == op
    assert got.payload == pkt.payload and got.padding == pkt.padding
    assert encode(got) == raw


def test_read_request_has_reth_and_no_payload():
    pkt = Packet(base=base(Op.READ), data=meth(), reth=Reth(0xABCD, 5, 4096))
    raw = encode(pkt)
    assert len(raw) == 12 + 20 + 16 + 4
    assert decode(raw).reth == Reth(0xABCD, 5, 4096)


def test_atomics_carry_meth():
    a = Packet(base=base(Op.CMP_SWAP), data=meth(posn=0), atomic=AtomicEth(0x10, 1, 2, 3))
    raw = encode(a)
    assert len(raw) == 12 + 20 + 28 + 4
    assert decode(raw).atomic == AtomicEth(0x10, 1, 2, 3)
    r = Packet(base=base(Op.ATOMIC_ACK), data=meth(posn=0), atomic_orig=0x1122334455667788)
    assert decode(encode(r)).atomic_orig == 0x1122334455667788


def test_padding_longer_than_payload_is_rejected():
    raw = encode(Packet(base=base(Op.SEND), data=meth(pad=9), payload=b"ab"))
    with pytest.raises(DecodeError):
        decode(raw)


def test_non_metaroce_opcode_is_rejected():
    raw = encode(Packet(base=BaseHeader(0x04, 0, 1, 2), data=meth()))  # RoCEv2 RC
    with pytest.raises(DecodeError):
        decode(raw)


# -------------------------------------------------------------------- ACKs


def test_ack_layout_and_fields():
    ack = AckHeader(cum_fsn=1, max_fsn=7, status=1, cnp=3, tag=2, cdmsn=9,
                    active_ports=0xF, rate_hint=400, echo_ts=123,
                    fsn_vec=make_fsn_vec(1, [3, 4, 7]))
    raw = encode(Packet(base=base(Op.SEL_ACK), ack=ack))
    assert len(raw) == 12 + 20 + 32 + 4
    got = decode(raw).ack
    assert got.kind == AckKind.SACK
    assert got.cnp == 3 and got.active_ports == 0xF and got.rate_hint == 400
    assert sorted(got.sacked_fsns()) == [3, 4, 7]


def test_sack_vector_matches_spec_figure_4_8():
    # CumulativeFSN=1, SelAckBitVector=100110 (bit 0 = FSN 2) -> FSNs 3, 4, 7.
    vec = (0b100110).to_bytes(4, "big") + bytes(28)
    assert sorted(AckHeader(cum_fsn=1, max_fsn=7, status=1, fsn_vec=vec).sacked_fsns()) == [3, 4, 7]


def test_sack_vector_wraps_fsn():
    vec = make_fsn_vec(0xFFFE, [0xFFFF + 2])  # FSN 1 after wrap
    assert AckHeader(cum_fsn=0xFFFE, max_fsn=1, status=1, fsn_vec=vec).sacked_fsns() == [1]


def test_shortened_sack_vector_round_trips():
    ack = AckHeader(cum_fsn=5, max_fsn=9, status=1, fsn_vec=(0b1000).to_bytes(4, "big"))
    raw = encode(Packet(base=base(Op.SEL_ACK), ack=ack))
    got = decode(raw)
    assert got.ack.fsn_vec == (0b1000).to_bytes(4, "big")
    assert got.ack.sacked_fsns() == [9]
    assert encode(got) == raw


def test_brnr_splits_vectors():
    status = 0b010_00011  # BRNR, backoff code 3
    ack = AckHeader(cum_fsn=10, max_fsn=14, status=status,
                    fsn_vec=make_fsn_vec(10, [12]), rnr_vec=make_fsn_vec(10, [13, 14]))
    got = decode(encode(Packet(base=base(Op.SEL_ACK), ack=ack))).ack
    assert got.kind == AckKind.BRNR
    assert got.rnr_backoff_us == 40  # 5 us << 3
    assert got.sacked_fsns() == [12]
    assert sorted(got.rnr_fsns()) == [13, 14]


@pytest.mark.parametrize("status,kind", [(0, AckKind.ACK), (1, AckKind.SACK),
                                         (0x21, AckKind.HRNR), (0x61, AckKind.NAK)])
def test_ack_kinds(status, kind):
    assert AckHeader(cum_fsn=0, max_fsn=0, status=status).kind == kind


# ------------------------------------------------------- control, RNR-cancel


def test_ping_round_trip():
    pkt = Packet(base=base(Op.UNRELIABLE_CONTROL, flow=0),
                 control=ControlHeader(type=0, resp_id=42, ts=99),
                 ping=PingHeader(tag=1, error_state=0, flow_count=16, active_ports=0xF),
                 payload=b"echo")
    got = decode(encode(pkt))
    assert got.data is None
    assert got.control.resp_id == 42 and got.ping.flow_count == 16 and got.payload == b"echo"


def test_rnr_cancel_round_trip():
    pkt = Packet(base=base(Op.RNR_CANCEL), rnr_cancel=RnrCancel(8, 2, 300, rate_hint=200))
    raw = encode(pkt)
    assert len(raw) == 12 + 8 + 4
    assert decode(raw).rnr_cancel == RnrCancel(8, 2, 300, rate_hint=200)


# -------------------------------------------------------------------- pcap


def _pcap(frames):
    out = io.BytesIO()
    out.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
    for i, (src, dst, sport, dport, ecn, payload) in enumerate(frames):
        udp = struct.pack("!HHHH", sport, dport, 8 + len(payload), 0) + payload
        ip6 = struct.pack("!IHBB", (6 << 28) | (ecn << 20), len(udp), 17, 64) + src + dst + udp
        eth = b"\x02" * 6 + b"\x04" * 6 + b"\x86\xdd" + ip6
        out.write(struct.pack("<IIII", 1, i * 10, len(eth), len(eth)) + eth)
    out.seek(0)
    return out


def test_pcap_summary_pairs_data_and_acks():
    a, b = bytes(15) + b"\x01", bytes(15) + b"\x02"
    data = [encode(Packet(base=base(Op.WRITE, flow=1, dqp=0x22, sqp=0x11),
                          data=meth(fsn=i, retx=(i == 2)), reth=Reth(0, 1, 4096),
                          payload=b"p" * 100))
            for i in range(3)]
    ack = encode(Packet(base=base(Op.SEL_ACK, flow=1, dqp=0x11, sqp=0x22),
                        ack=AckHeader(cum_fsn=2, max_fsn=2, cnp=2, rate_hint=100)))
    f = _pcap([(a, b, 49152, 2766, 0, data[0]), (a, b, 49152, 2766, 3, data[1]),
               (a, b, 49200, 2766, 0, data[2]), (b, a, 49152, 2766, 0, ack)])
    d = to_dict(summarize(read_frames(f)))
    assert d["decode_errors"] == 0 and d["mcrc_bad"] == 0
    (flow,) = d["flows"]
    assert flow["data_pkts"] == 3 and flow["retx"] == 1 and flow["ce_marked"] == 1
    assert flow["sports"] == [49152, 49200]
    assert flow["acks"] == 1 and flow["cnp_total"] == 2 and flow["rate_hint_last"] == 100
