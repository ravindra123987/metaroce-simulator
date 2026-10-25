"""Our codec against packets captured from libsoftmetaroce (metaroce @ 98686f59).

lsmr-write-16k.pcap: rdma_gen --op_type=write --message_size=16384, one plane,
full-size capture (first 160 frames), h00 -> h07 on the 2p-2x4 lab.
"""

import struct
from pathlib import Path

from metaroce_sim.codec import AckKind, Op, decode, encode
from metaroce_sim.codec.pcap import read_frames

FIXTURE = Path(__file__).parent / "fixtures/pcap/lsmr-write-16k.pcap"


def _udp_payloads():
    data = FIXTURE.read_bytes()
    off = 24
    while off < len(data):
        _, _, cap, _ = struct.unpack_from("<IIII", data, off)
        frame = data[off + 16:off + 16 + cap]
        off += 16 + cap
        ulen = struct.unpack_from("!H", frame, 14 + 40 + 4)[0]
        yield frame[14 + 40 + 8:14 + 40 + ulen]


def test_every_lsmr_packet_decodes_with_valid_mcrc():
    frames = list(read_frames(FIXTURE.open("rb")))
    assert len(frames) == 160
    assert all(f.error is None for f in frames)
    assert all(f.packet.mcrc_valid for f in frames)
    ops = {f.packet.op for f in frames}
    assert ops == {Op.WRITE, Op.SEL_ACK}


def test_lsmr_packets_round_trip_byte_for_byte():
    for raw in _udp_payloads():
        assert encode(decode(raw)) == raw


def test_lsmr_write_fields():
    frames = list(read_frames(FIXTURE.open("rb")))
    w = next(f for f in frames if f.packet.op == Op.WRITE)
    assert w.dport == 2766 and 49152 <= w.sport <= 65535
    assert w.packet.reth.length == 16384 and len(w.packet.payload) == 4096
    acks = [f.packet.ack for f in frames if f.packet.ack]
    assert acks and all(a.kind in (AckKind.ACK, AckKind.SACK) for a in acks)
