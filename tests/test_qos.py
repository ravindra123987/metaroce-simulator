from metaroce_sim.emu import faults, qos
from metaroce_sim.scenario import Fault
from metaroce_sim.topo.model import Topology

T = Topology(name="t", planes=2, spines=2, leaves=4, hosts_per_leaf=2)
ECN = {"shape": {"ports": "downlinks", "rate": "1gbit"},
       "ecn": {"min_kb": 30, "max_kb": 90, "prob": 0.1, "limit_kb": 400}}


def test_port_sets():
    assert len(qos.switch_ports(T, "downlinks")) == 2 * 4 * 2
    assert len(qos.switch_ports(T, "uplinks")) == 2 * 4 * 2
    assert len(qos.switch_ports(T, "spines")) == 2 * 2 * 4
    assert len(qos.switch_ports(T, "all")) == 48


def test_red_ecn_setup():
    q = qos.from_fabric(T, ECN)
    node, cmd = q.setup()[0]
    assert node.startswith("p0-leaf0")
    assert "htb rate 1gbit ceil 1gbit" in cmd
    assert "red limit 409600 min 30720 max 92160" in cmd and cmd.endswith("ecn")


def test_drop_tail_without_ecn():
    q = qos.from_fabric(T, {"shape": {"ports": "downlinks", "rate": "1gbit"}, "limit_kb": 100})
    assert "bfifo limit 102400" in q.setup()[0][1]


def test_no_shape_no_qos():
    assert qos.from_fabric(T, {}) is None


def test_netem_on_shaped_port_keeps_shaper():
    q = qos.from_fabric(T, ECN)
    cmds = dict(faults.commands(T, Fault(1, "netem", {"plane": 1}, "loss 10%"), q))
    assert "parent 1:10 handle 10: netem loss 10%" in cmds["p1-leaf3"]
    assert "root netem loss 10%" in cmds["h07"]  # host NICs are not shaped
    clear = dict(faults.commands(T, Fault(2, "clear", {"plane": 1}), q))
    assert " red " in clear["p1-leaf3"] and "root" not in clear["p1-leaf3"]
