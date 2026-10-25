"""Fabric shaping and ECN marking on switch egress ports.

veths are effectively infinitely fast, so queues never build unless a port
is rate limited. Each shaped port gets:

    root htb 1:  ->  class 1:10 (rate R)  ->  child 10:
                                               red ... ecn   (ecn block given)
                                               bfifo         (drop-tail otherwise)

Faults on a shaped port replace child 10: with netem and `clear` puts the
child back, so shaping survives fault injection.

Scenario block:

    fabric:
      shape:
        ports: downlinks      # downlinks (leaf -> host) | uplinks | spines | all
        rate: 1gbit
      ecn:                    # omit for drop-tail
        min_kb: 30
        max_kb: 90
        prob: 0.1
        limit_kb: 400
"""

from __future__ import annotations

from dataclasses import dataclass

from metaroce_sim.topo.model import Topology

AVPKT = 4200  # ~one 4 KiB MetaRoCE data packet on the wire
PORT_SETS = {"downlinks", "uplinks", "spines", "all"}


@dataclass(frozen=True)
class Qos:
    ports: tuple[str, ...]  # "node:iface"
    rate: str
    ecn: dict | None
    limit_bytes: int

    def child(self, iface: str) -> str:
        if self.ecn:
            mn, mx = int(self.ecn["min_kb"] * 1024), int(self.ecn["max_kb"] * 1024)
            burst = (2 * mn + mx) // (3 * AVPKT) + 1
            return (f"tc qdisc replace dev {iface} parent 1:10 handle 10: red "
                    f"limit {self.limit_bytes} min {mn} max {mx} avpkt {AVPKT} "
                    f"burst {burst} probability {self.ecn.get('prob', 0.1)} "
                    f"bandwidth {self.rate} ecn")
        return f"tc qdisc replace dev {iface} parent 1:10 handle 10: bfifo limit {self.limit_bytes}"

    def setup(self) -> list[tuple[str, str]]:
        out = []
        for ep in self.ports:
            node, iface = ep.split(":")
            out.append((node, "; ".join([
                f"tc qdisc del dev {iface} root 2>/dev/null",
                f"tc qdisc add dev {iface} root handle 1: htb default 10",
                (f"tc class add dev {iface} parent 1: classid 1:10 htb rate {self.rate} "
                f"ceil {self.rate}"),
                self.child(iface),
            ])))
        return out

    def teardown(self) -> list[tuple[str, str]]:
        return [(ep.split(":")[0], f"tc qdisc del dev {ep.split(':')[1]} root 2>/dev/null; true")
                for ep in self.ports]

    def shaped(self, endpoint: str) -> bool:
        return endpoint in self.ports


def switch_ports(t: Topology, which: str) -> list[str]:
    if which not in PORT_SETS:
        raise ValueError(f"shape.ports must be one of {sorted(PORT_SETS)}")
    out = []
    for p in range(t.planes):
        for lf in range(t.leaves):
            if which in ("downlinks", "all"):
                out += [f"{t.leaf(p, lf)}:{t.leaf_downlink(k)}" for k in range(t.hosts_per_leaf)]
            if which in ("uplinks", "all"):
                out += [f"{t.leaf(p, lf)}:{t.leaf_uplink(s)}" for s in range(t.spines)]
        if which in ("spines", "all"):
            out += [f"{t.spine(p, s)}:{t.spine_port(lf)}"
                    for s in range(t.spines) for lf in range(t.leaves)]
    return out


def from_fabric(t: Topology, fabric: dict) -> Qos | None:
    shape = (fabric or {}).get("shape")
    if not shape:
        return None
    ecn = fabric.get("ecn")
    limit_kb = (ecn or {}).get("limit_kb", fabric.get("limit_kb", 400))
    return Qos(tuple(switch_ports(t, shape.get("ports", "downlinks"))), str(shape["rate"]),
               ecn, int(limit_kb * 1024))
