import json
import struct

from metaroce_sim.analysis import report, timeline
from metaroce_sim.codec import Op, Packet, encode
from metaroce_sim.codec.headers import BaseHeader, DataHeader, Reth, opcode


def _ip6(s):
    import ipaddress

    return ipaddress.IPv6Address(s).packed


def _pcap(path, frames):
    out = [struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)]
    for ts, src, dst, sport, ecn, payload in frames:
        udp = struct.pack("!HHHH", sport, 2766, 8 + len(payload), 0) + payload
        ip6 = struct.pack("!IHBB", (6 << 28) | (ecn << 20), len(udp), 17, 64) + _ip6(src) + _ip6(dst) + udp
        eth = b"\x02" * 6 + b"\x04" * 6 + b"\x86\xdd" + ip6
        sec, usec = int(ts), round((ts % 1) * 1e6)
        out.append(struct.pack("<IIII", sec, usec, len(eth), len(eth)) + eth)
    path.write_bytes(b"".join(out))


def _data(fsn, retx=False, timeout=False):
    return encode(Packet(base=BaseHeader(opcode(Op.WRITE), 0, 2, 2),
                         data=DataHeader(fsn=fsn, una=0, posn=0, msn=0, retx=retx, timeout=timeout),
                         reth=Reth(0, 1, 4096), payload=b"x" * 4096))


def test_run_report(tmp_path):
    run = tmp_path / "r"
    (run / "h00").mkdir(parents=True)
    (run / "h07").mkdir()
    sc = {"name": "t", "topology": "topologies/2p-2x4.yaml", "duration": 4.0,
          "traffic": [{"pairs": [[0, 7]]}]}
    (run / "scenario.json").write_text(json.dumps(sc))
    wall0 = 1000.0
    ev = [{"t": 0, "kind": "start", "wall0": wall0}, {"t": 1.0, "kind": "clients-start"},
          {"t": 3.0, "kind": "fault", "action": "netem", "target": {"plane": 0}, "spec": "loss 30%"},
          {"t": 4.0, "kind": "fault", "action": "clear", "target": {"plane": 0}, "spec": ""}]
    (run / "events.jsonl").write_text("\n".join(json.dumps(e) for e in ev))
    start = wall0 + 1.0
    # 100 packets/s for 4 s from h00 plane 0 to h07; drop to 0 in [2, 2.5); re-steer at 2.5.
    frames_rx, frames_tx = [], []
    for i in range(400):
        t = start + i / 100
        sport = 49152 if t < start + 2.5 else 49153
        retx = start + 2.5 <= t < start + 2.6
        pkt = _data(i, retx=retx, timeout=retx)
        frames_tx.append((t, "fd00::2", "fd00:0:3:1::2", sport, 2, pkt))
        if not (start + 2.0 <= t < start + 2.5):
            frames_rx.append((t + 0.0001, "fd00::2", "fd00:0:3:1::2", sport, 3 if i % 10 == 0 else 2, pkt))
    _pcap(run / "h00" / "eth1.pcap", frames_tx)
    _pcap(run / "h07" / "eth1.pcap", frames_rx)

    d = timeline.analyze(run)
    gp = d["goodput_gbps"]["plane 0"]
    assert abs(gp[5] - 100 * (4096 + 32 + 16 + 4) * 8 / 1e9) < 1e-3
    assert gp[22] == 0  # dropped window
    (flow,) = d["flows"].values()
    assert [p for _, p in flow["sports"]] == [49152, 49153] and flow["rto"] == 10
    assert d["faults"][0]["t"] == 2.0
    assert d["recovery"][0]["recover_s"] is not None
    assert sum(d["ce_per_s"]["plane 0"]) > 0

    _, html = report.write(run)
    text = html.read_text()
    assert "<title>t run report</title>" in text and "c-goodput" in text


def test_recovery_ignores_end_of_run_drop():
    from metaroce_sim.analysis.timeline import recovery

    total = [2.0] * 50 + [1.0] * 10 + [2.0] * 80 + [0.0] * 50  # dip at 5 s, traffic ends at 14 s
    r = recovery(total, 5.0, t_clear=7.0, duration=14.0)
    assert r["min_gbps"] == 1.0 and r["recover_s"] is not None and r["recover_s"] <= 1.0
    flat = recovery([2.0] * 200, 5.0, t_clear=7.0, duration=15.0)
    assert flat["recover_s"] == 0.0 and flat["min_gbps"] == 2.0
