"""Generate a containerlab lab from a Topology.

Setup scripts are idempotent (`replace`, not `add`), so faults can re-run
them to restore routes after a link comes back.

Output directory layout:

    <out>/<name>.clab.yaml      containerlab topology
    <out>/config/<node>.sh      per-node setup (addresses, routes, sysctls),
                                bind-mounted and run at deploy via `exec`

Switches forward in the Linux kernel. ECMP hashes on the L4 5-tuple
(`fib_multipath_hash_policy=1`), so a MetaRoCE flow's UDP source port
(its entropy value) picks the spine.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .model import Topology

SWITCH_SYSCTLS = [
    "net.ipv6.conf.all.forwarding=1",
    "net.ipv6.fib_multipath_hash_policy=1",
    # A local port going down drops its ECMP next hop (like BFD on a real
    # switch). Failures further away stay blackholes: routes are static.
    "net.ipv6.conf.all.ignore_routes_with_linkdown=1",
    "net.ipv6.conf.all.accept_dad=0",
    "net.ipv6.conf.default.accept_dad=0",
    # Linux drops IPv6 addresses (and the static routes using them) when a
    # link is set down; a link-down fault must not erase configuration.
    "net.ipv6.conf.all.keep_addr_on_down=1",
    "net.ipv6.conf.default.keep_addr_on_down=1",
]

HOST_SYSCTLS = [
    "net.ipv6.conf.all.accept_dad=0",
    "net.ipv6.conf.default.accept_dad=0",
    "net.ipv6.conf.all.keep_addr_on_down=1",
    "net.ipv6.conf.default.keep_addr_on_down=1",
]


def _sysctl(lines: list[str], keys: list[str]) -> None:
    lines += [f"sysctl -qw {k}" for k in keys]


def _up(lines: list[str], iface: str, mtu: int) -> None:
    # Per-interface settings: containerlab creates the interfaces before this
    # script runs, so the all/default sysctls do not reach them. Without these,
    # a link coming back up stays dark for ~2 s: its addresses go tentative
    # for DAD, and the flushed neighbor is only re-solicited after the default
    # 1 s retransmit timer (measured: 2.3 s -> <0.3 s with both fixed).
    lines.append(f"sysctl -qw net.ipv6.conf.{iface}.accept_dad=0 "
                 f"net.ipv6.conf.{iface}.dad_transmits=0 "
                 f"net.ipv6.neigh.{iface}.retrans_time_ms=100")
    lines.append(f"ip link set {iface} mtu {mtu} up")


def leaf_script(t: Topology, p: int, lf: int) -> str:
    lines = ["#!/bin/sh", "set -e"]
    _sysctl(lines, SWITCH_SYSCTLS)
    for s in range(t.spines):
        ifc = t.leaf_uplink(s)
        _up(lines, ifc, t.link.mtu)
        lines.append(f"ip -6 addr replace {t.fabric_addr(p, s, lf, 'leaf')}/64 dev {ifc} nodad")
    for k in range(t.hosts_per_leaf):
        ifc = t.leaf_downlink(k)
        n = lf * t.hosts_per_leaf + k
        _up(lines, ifc, t.link.mtu)
        lines.append(f"ip -6 addr replace {t.host_gw(n, p)}/64 dev {ifc} nodad")
    next_hops = " ".join(
        f"nexthop via {t.fabric_addr(p, s, lf, 'spine')} dev {t.leaf_uplink(s)}"
        for s in range(t.spines)
    )
    for other in range(t.leaves):
        if other != lf:
            lines.append(f"ip -6 route replace {t.leaf_prefix(p, other)} {next_hops}")
    return "\n".join(lines) + "\n"


def spine_script(t: Topology, p: int, s: int) -> str:
    lines = ["#!/bin/sh", "set -e"]
    _sysctl(lines, SWITCH_SYSCTLS)
    for lf in range(t.leaves):
        ifc = t.spine_port(lf)
        _up(lines, ifc, t.link.mtu)
        lines.append(f"ip -6 addr replace {t.fabric_addr(p, s, lf, 'spine')}/64 dev {ifc} nodad")
        lines.append(
            f"ip -6 route replace {t.leaf_prefix(p, lf)} "
            f"via {t.fabric_addr(p, s, lf, 'leaf')} dev {ifc}"
        )
    return "\n".join(lines) + "\n"


def host_script(t: Topology, n: int) -> str:
    lines = ["#!/bin/sh", "set -e"]
    _sysctl(lines, HOST_SYSCTLS)
    for p in range(t.planes):
        ifc = t.host_nic(p)
        _up(lines, ifc, t.link.mtu)
        lines.append(f"ip -6 addr replace {t.host_addr(n, p)}/64 dev {ifc} nodad")
        lines.append(f"ip -6 route replace {t.plane_prefix(p)} via {t.host_gw(n, p)} dev {ifc}")
    return "\n".join(lines) + "\n"


def scripts(t: Topology) -> dict[str, str]:
    out = {}
    for p in range(t.planes):
        for s in range(t.spines):
            out[t.spine(p, s)] = spine_script(t, p, s)
        for lf in range(t.leaves):
            out[t.leaf(p, lf)] = leaf_script(t, p, lf)
    for n in range(t.num_hosts):
        out[t.host(n)] = host_script(t, n)
    return out


def clab(t: Topology) -> dict:
    nodes = {}
    for name in scripts(t):
        image = t.host_image if name.startswith("h") else t.switch_image
        nodes[name] = {
            "kind": "linux",
            "image": image,
            "binds": [f"config/{name}.sh:/etc/metaroce/setup.sh:ro"],
            "exec": ["sh /etc/metaroce/setup.sh"],
            "labels": {"metaroce.role": "host" if name.startswith("h") else "switch"},
        }
    return {
        "name": f"metaroce-{t.name}",
        "topology": {
            "nodes": nodes,
            "links": [{"endpoints": [a, b]} for a, b in t.links()],
        },
    }


def write(t: Topology, out: str | Path) -> Path:
    out = Path(out)
    (out / "config").mkdir(parents=True, exist_ok=True)
    for name, body in scripts(t).items():
        path = out / "config" / f"{name}.sh"
        path.write_text(body)
        path.chmod(0o755)
    clab_path = out / f"{t.name}.clab.yaml"
    clab_path.write_text(yaml.safe_dump(clab(t), sort_keys=False))
    return clab_path
