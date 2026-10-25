from collections import Counter

import pytest

from metaroce_sim.sim.engine import Sim
from metaroce_sim.sim.fabric import Fabric, SimParams, rate_bps
from metaroce_sim.sim.net import CE, Packet, Red
from metaroce_sim.topo.model import Topology

T = Topology(name="t", planes=2, spines=2, leaves=4, hosts_per_leaf=2)


def test_events_run_in_time_order_and_cancel():
    sim, seen = Sim(), []
    sim.after(30, seen.append, "c")
    sim.after(10, seen.append, "a")
    ev = sim.after(20, seen.append, "x")
    sim.after(20, seen.append, "b")
    Sim.cancel(ev)
    sim.run()
    assert seen == ["a", "b", "c"] and sim.now == 30


def test_rate_parse():
    assert rate_bps("100gbit") == 100e9 and rate_bps("25G") == 25e9 and rate_bps(1e6) == 1e6


def _fabric(**kw):
    sim = Sim()
    return sim, Fabric(sim, T, SimParams(**kw))


def test_one_way_latency_is_serialization_plus_propagation():
    sim, fab = _fabric(rate_bps=100e9, prop_ns=500)
    got = []
    dst = fab.hosts[7]
    dst.handlers[2766] = lambda pkt, plane: got.append(sim.now)
    fab.hosts[0].send(Packet(fab.hosts[0].addr(0), dst.addr(0), 49152, 2766, 4096), 0)
    sim.run()
    # host -> leaf -> spine -> leaf -> host: 4 links, each 4096B at 100G (328 ns) + 500 ns.
    assert got == [4 * (328 + 500)]


def test_ecmp_spreads_by_source_port_and_pins_each_port():
    sim, fab = _fabric()
    src, dst = fab.hosts[0], fab.hosts[7]
    paths = {}
    dst.handlers[2766] = lambda pkt, plane: paths.setdefault(pkt.sport, set()).add(pkt.hops[1])
    for sport in range(49152, 49152 + 256):
        for _ in range(2):
            src.send(Packet(src.addr(0), dst.addr(0), sport, 2766, 256), 0)
    sim.run()
    assert all(len(v) == 1 for v in paths.values())
    per_spine = Counter(next(iter(v)) for v in paths.values())
    assert set(per_spine) == {"p0-spine0", "p0-spine1"}
    assert min(per_spine.values()) > 90


def test_local_link_down_drops_next_hop_remote_link_down_blackholes():
    sim, fab = _fabric()
    src, dst = fab.hosts[0], fab.hosts[7]  # leaf 0 -> leaf 3
    got = Counter()
    dst.handlers[2766] = lambda pkt, plane: got.update([pkt.hops[1]])

    def burst():
        for sport in range(49152, 49152 + 200):
            src.send(Packet(src.addr(0), dst.addr(0), sport, 2766, 256), 0)

    fab.set_link("p0-leaf0:eth1", False)  # leaf0 -> spine0, local to the sender's leaf
    burst()
    sim.run()
    assert got == Counter({"p0-spine1": 200})

    got.clear()
    fab.set_link("p0-leaf0:eth1", True)
    fab.set_link("p0-spine0:eth4", False)  # spine0 -> leaf3, remote: static route keeps using it
    burst()
    sim.run()
    assert got["p0-spine0"] == 0 and 60 < got["p0-spine1"] < 140


def test_red_marks_ce_when_queue_builds():
    red = Red(min_th=20_000, max_th=60_000, max_p=1.0, limit=200_000)
    sim, fab = _fabric(rate_bps=10e9, red=red)
    srcs, dst = fab.hosts[:4], fab.hosts[7]
    marks = Counter()
    dst.handlers[2766] = lambda pkt, plane: marks.update([pkt.ecn == CE])
    for i in range(50):  # 4 senders x 50 x 4 KiB into one 10G downlink
        for s in srcs:
            s.send(Packet(s.addr(0), dst.addr(0), 49152 + i, 2766, 4096), 0)
    sim.run()
    assert marks[True] > 0 and marks[False] > 0
    # Each CE was marked by one queue; a marked packet may still be dropped later.
    assert 0 < marks[True] <= sum(pt.stats["ce"] for pt in fab.ports.values())
    assert fab.ports["p0-leaf3:eth4"].stats["ce"] > 0


def test_queue_limit_drops():
    sim, fab = _fabric(rate_bps=1e9, queue_bytes=16_384)
    s, d = fab.hosts[0], fab.hosts[1]  # same leaf
    for i in range(20):
        s.send(Packet(s.addr(0), d.addr(0), 49152, 2766, 4096), 0)
    sim.run()
    assert fab.ports["h00:eth1"].stats["drop_q"] == 20 - 4


def test_schedule_in_past_rejected():
    sim = Sim()
    sim.run(until=100)
    with pytest.raises(ValueError):
        sim.at(50, print)


def test_source_routed_packets_take_the_named_spine():
    sim, fab = _fabric()
    src, dst = fab.hosts[0], fab.hosts[7]
    seen = []
    dst.handlers[2766] = lambda pkt, plane: seen.append(pkt.hops[1])
    for via in (0, 1, 1, 0):
        src.send(Packet(src.addr(0), dst.addr(0), 49152, 2766, 256, via=via), 0)
    sim.run()
    assert sorted(seen) == ["p0-spine0", "p0-spine0", "p0-spine1", "p0-spine1"]
    fab.set_link("p0-leaf0:eth1", False)  # pinned path through a down uplink is dropped
    seen.clear()
    src.send(Packet(src.addr(0), dst.addr(0), 49152, 2766, 256, via=0), 0)
    sim.run()
    assert seen == []


def test_trimming_switch_forwards_headers_instead_of_dropping():
    sim, fab = _fabric(rate_bps=1e9, queue_bytes=16_384)
    nic = fab.ports["h00:eth1"]
    nic.trim_bytes = 64
    s, d = fab.hosts[0], fab.hosts[1]
    got = Counter()
    d.handlers[2766] = lambda pkt, plane: got.update(["trim" if pkt.trimmed else "full"])
    for _ in range(20):
        s.send(Packet(s.addr(0), d.addr(0), 49152, 2766, 4096), 0)
    sim.run()
    assert got["trim"] == 16 and got["full"] == 4 and nic.stats["drop_q"] == 0
