"""Fabric checks against a deployed lab. Run on the containerlab host:

    mrsim emu up topologies/2p-2x4.yaml
    pytest -m lab tests/lab
"""

import pytest

from metaroce_sim.emu import lab
from metaroce_sim.topo import model

pytestmark = pytest.mark.lab

TOPO = model.load("topologies/2p-2x4.yaml")


@pytest.mark.parametrize("plane", range(TOPO.planes))
def test_source_ports_spread_across_spines(plane):
    r = lab.ecmp_check(TOPO, 0, TOPO.num_hosts - 1, plane, ports=256)
    assert r["p_value"] > 0.01, r


def test_one_source_port_pins_to_one_spine():
    r = lab.ecmp_check(TOPO, 0, TOPO.num_hosts - 1, 0, ports=1, reps=20, first_port=50001)
    assert max(r["per_spine"].values()) >= 20, r


def test_link_flap_leaves_fabric_healthy():
    from metaroce_sim.emu import faults
    from metaroce_sim.emu.lab import dexec
    from metaroce_sim.scenario import Fault

    for action in ("link-down", "link-up"):
        for node, cmd in faults.commands(TOPO, Fault(0, action, {"link": "p0-spine0:eth4"})):
            dexec(TOPO, node, cmd)
    assert lab.health(TOPO, [(0, TOPO.num_hosts - 1)]) == []
