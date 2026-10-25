"""Fault injection on the emulated fabric.

Targets name switch or host ports:

    {link: "p0-spine1:eth3"}     one port (and its peer, unless one_sided: true)
    {node: "p1-spine0"}          every data port of a node
    {plane: 1}                   every host-facing port in a plane
                                 (leaf downlinks and the hosts' plane NICs)

Actions: link-down, link-up, netem (tc netem `spec` on egress), clear
(removes netem and brings links back up). link-up and clear re-run the
node's idempotent setup script, because the kernel drops routes whose
next-hop interface went down.
"""

from __future__ import annotations

from metaroce_sim.scenario import Fault
from metaroce_sim.topo.model import Topology

RESETUP = "sh /etc/metaroce/setup.sh >/dev/null 2>&1"


def _peers(t: Topology) -> dict[str, str]:
    out = {}
    for a, b in t.links():
        out[a], out[b] = b, a
    return out


def _node_ports(t: Topology, node: str) -> list[str]:
    return [e for pair in t.links() for e in pair if e.split(":")[0] == node]


def ports(t: Topology, target: dict) -> list[str]:
    """Resolve a target to a sorted list of "node:iface" endpoints."""
    if "link" in target:
        ep = target["link"]
        peers = _peers(t)
        if ep not in peers:
            raise ValueError(f"unknown port {ep!r}")
        return sorted({ep} if target.get("one_sided") else {ep, peers[ep]})
    if "node" in target:
        eps = _node_ports(t, target["node"])
        if not eps:
            raise ValueError(f"unknown node {target['node']!r}")
        return sorted(eps)
    if "plane" in target:
        p = int(target["plane"])
        if not 0 <= p < t.planes:
            raise ValueError(f"plane {p} out of range")
        eps = set()
        for n in range(t.num_hosts):
            lf, k = t.host_leaf(n)
            eps.add(f"{t.host(n)}:{t.host_nic(p)}")
            eps.add(f"{t.leaf(p, lf)}:{t.leaf_downlink(k)}")
        return sorted(eps)
    raise ValueError(f"target needs link, node or plane: {target!r}")


def commands(t: Topology, fault: Fault, qos=None) -> list[tuple[str, str]]:
    """(node, shell command) pairs that apply `fault`.

    On a port shaped by `qos` (metaroce_sim.emu.qos.Qos), netem replaces the
    shaper's child qdisc and `clear` restores it, so the rate limit stays.
    """
    out = []
    for ep in ports(t, fault.target):
        node, iface = ep.split(":")
        shaped = qos is not None and qos.shaped(ep)
        if fault.action == "link-down":
            out.append((node, f"ip link set {iface} down"))
        elif fault.action == "link-up":
            out.append((node, f"ip link set {iface} up; {RESETUP}"))
        elif fault.action == "netem" and shaped:
            out.append((node, (f"tc qdisc replace dev {iface} parent 1:10 handle 10: "
                              f"netem {fault.spec}")))
        elif fault.action == "netem":
            out.append((node, f"tc qdisc replace dev {iface} root netem {fault.spec}"))
        elif fault.action == "clear" and shaped:
            out.append((node, f"{qos.child(iface)}; ip link set {iface} up; {RESETUP}"))
        elif fault.action == "clear":
            out.append((node, (f"tc qdisc del dev {iface} root 2>/dev/null; "
                              f"ip link set {iface} up; {RESETUP}")))
        else:
            raise ValueError(f"unknown action {fault.action!r}")
    return out
