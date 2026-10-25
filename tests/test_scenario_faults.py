import pytest

from metaroce_sim.emu import faults
from metaroce_sim.scenario import Fault, ScenarioError, parse, seconds, size_bytes
from metaroce_sim.topo.model import Topology

T = Topology(name="t", planes=2, spines=2, leaves=4, hosts_per_leaf=2)


def scenario(**over):
    d = {
        "name": "s", "topology": "topologies/2p-2x4.yaml", "duration": "20s",
        "traffic": [{"pairs": [{"src": "h00", "dst": "h07"}], "op": "write", "msg_size": "1MiB"}],
        "faults": [{"at": "5s", "action": "netem", "target": {"plane": 1}, "spec": "loss 30%"},
                   {"at": "2s", "action": "link-down", "target": {"link": "p0-spine1:eth1"}}],
    }
    d.update(over)
    return d


def test_units():
    assert seconds("500ms") == 0.5 and seconds("10us") == pytest.approx(1e-5) and seconds(3) == 3.0
    assert size_bytes("1MiB") == 1 << 20 and size_bytes("64KB") == 64000 and size_bytes(10) == 10


def test_parse_sorts_faults_and_resolves_hosts():
    sc = parse(scenario(), T.num_hosts)
    assert sc.traffic[0].pairs == [(0, 7)]
    assert [f.at for f in sc.faults] == [2.0, 5.0]


@pytest.mark.parametrize("pairs,n", [("all-to-all", 56), ("ring", 8), ({"incast": 0, "senders": 4}, 4),
                                     ({"shift": 4}, 8), ({"permutation": 3}, 8)])
def test_pair_shortcuts(pairs, n):
    sc = parse(scenario(traffic=[{"pairs": pairs}]), T.num_hosts)
    assert len(sc.traffic[0].pairs) == n


def test_permutation_is_a_seeded_derangement():
    a = parse(scenario(traffic=[{"pairs": {"permutation": 3}}]), T.num_hosts).traffic[0].pairs
    b = parse(scenario(traffic=[{"pairs": {"permutation": 3}}]), T.num_hosts).traffic[0].pairs
    assert a == b and all(s != d for s, d in a)
    assert sorted(d for _, d in a) == list(range(T.num_hosts))


@pytest.mark.parametrize("bad", [
    {"traffic": [{"pairs": {"shift": 8}}]},
    {"traffic": [{"op": "teleport"}]},
    {"traffic": [{"pairs": [[0, 0]]}]},
    {"traffic": [{"pairs": [[0, 99]]}]},
    {"faults": [{"at": "30s", "action": "clear", "target": {"plane": 0}}]},
    {"faults": [{"at": "1s", "action": "netem", "target": {"plane": 0}}]},
])
def test_invalid(bad):
    with pytest.raises(ScenarioError):
        parse(scenario(**bad), T.num_hosts)


def test_link_target_includes_peer():
    assert faults.ports(T, {"link": "p0-spine1:eth1"}) == ["p0-leaf0:eth2", "p0-spine1:eth1"]
    assert faults.ports(T, {"link": "p0-spine1:eth1", "one_sided": True}) == ["p0-spine1:eth1"]


def test_plane_target_covers_every_host_facing_port():
    eps = faults.ports(T, {"plane": 1})
    assert len(eps) == 2 * T.num_hosts
    assert "h03:eth2" in eps and "p1-leaf1:eth4" in eps


def test_node_target():
    assert len(faults.ports(T, {"node": "p1-spine0"})) == T.leaves


def test_commands():
    cmds = faults.commands(T, Fault(1.0, "netem", {"link": "p0-spine1:eth1"}, "loss 30%"))
    assert ("p0-spine1", "tc qdisc replace dev eth1 root netem loss 30%") in cmds
    assert len(cmds) == 2
