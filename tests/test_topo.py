import yaml

from metaroce_sim.emu.lab import chi2_pvalue
from metaroce_sim.topo import gen, model

T = model.Topology(name="t", planes=2, spines=2, leaves=4, hosts_per_leaf=2)


def test_counts_and_links():
    assert T.num_hosts == 8
    links = T.links()
    assert len(links) == 2 * 4 * 2 + 8 * 2  # leaf-spine + host NICs
    ends = [e for pair in links for e in pair]
    assert len(ends) == len(set(ends)), "an interface is wired twice"


def test_host_wiring_is_multi_plane():
    # Host 5 sits on leaf 2 (slot 1); NIC p connects to leaf 2 of plane p.
    assert T.host_leaf(5) == (2, 1)
    assert ("h05:eth1", "p0-leaf2:eth4") in T.links()
    assert ("h05:eth2", "p1-leaf2:eth4") in T.links()
    assert T.host_addr(5, 1) == "fd00:1:2:1::2"


def test_leaf_routes_ecmp_over_all_spines():
    s = gen.leaf_script(T, 0, 1)
    assert "net.ipv6.fib_multipath_hash_policy=1" in s
    routes = [ln for ln in s.splitlines() if ln.startswith("ip -6 route replace")]
    assert len(routes) == T.leaves - 1
    for r in routes:
        assert r.count("nexthop via") == T.spines


def test_spine_routes_every_leaf():
    s = gen.spine_script(T, 1, 0)
    assert s.count("ip -6 route replace fd00:1:") == T.leaves


def test_host_routes_each_plane_via_its_nic():
    s = gen.host_script(T, 0)
    assert "ip -6 route replace fd00:0::/32 via fd00:0:0:0::1 dev eth1" in s
    assert "ip -6 route replace fd00:1::/32 via fd00:1:0:0::1 dev eth2" in s


def test_write(tmp_path):
    clab_file = gen.write(T, tmp_path)
    d = yaml.safe_load(clab_file.read_text())
    assert d["name"] == "metaroce-t"
    assert len(d["topology"]["nodes"]) == 2 * (2 + 4) + 8
    assert (tmp_path / "config" / "p0-leaf0.sh").exists()


def test_planes_limited_to_16():
    import pytest

    with pytest.raises(ValueError):
        model.Topology(name="x", planes=17, spines=1, leaves=1, hosts_per_leaf=1)


def test_chi2_pvalue():
    assert chi2_pvalue([500, 524]) > 0.1
    assert chi2_pvalue([1024, 0]) < 1e-6


def test_interfaces_disable_dad_and_resolve_neighbors_fast():
    for script in (gen.leaf_script(T, 0, 0), gen.spine_script(T, 0, 0), gen.host_script(T, 0)):
        ups = [ln for ln in script.splitlines() if ln.startswith("ip link set")]
        for ln in ups:
            iface = ln.split()[3]
            assert f"net.ipv6.conf.{iface}.accept_dad=0" in script
            assert f"net.ipv6.neigh.{iface}.retrans_time_ms=100" in script
