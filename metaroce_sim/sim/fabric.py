"""Build a simulated multi-plane Clos from the same Topology the emulator uses."""

from __future__ import annotations

import random
import re
import zlib
from dataclasses import dataclass

from metaroce_sim.topo.model import Topology

from .engine import Sim
from .net import Host, Port, Red, Switch, connect

_RATE = re.compile(r"^\s*([0-9.]+)\s*(k|m|g|t)?(bit|bps)?\s*$", re.IGNORECASE)


def rate_bps(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    m = _RATE.match(str(v))
    if not m:
        raise ValueError(f"bad rate {v!r}")
    mult = {None: 1, "k": 1e3, "m": 1e6, "g": 1e9, "t": 1e12}[(m.group(2) or "").lower() or None]
    return float(m.group(1)) * mult


@dataclass
class SimParams:
    rate_bps: float = 100e9
    prop_ns: int = 500
    queue_bytes: int = 1 << 20
    red: Red | None = None
    seed: int = 0  # mixed into each port's RNG (loss, RED marking)


class Fabric:
    def __init__(self, sim: Sim, topo: Topology, params: SimParams | None = None):
        self.sim, self.topo = sim, topo
        self.params = params or SimParams()
        self.switches: dict[str, Switch] = {}
        self.hosts: list[Host] = []
        self.ports: dict[str, Port] = {}  # "node:iface" -> Port, emulator names
        self._build()

    def _port(self, node, iface: str) -> Port:
        pr = self.params
        name = f"{node.name}:{iface}"
        port = Port(self.sim, node, name, pr.rate_bps, pr.prop_ns, red=pr.red,
                    limit_bytes=pr.queue_bytes,
                    rng=random.Random(zlib.crc32(name.encode()) ^ (pr.seed * 0x9E3779B1)))
        node.ports[iface] = port
        self.ports[port.name] = port
        return port

    def _build(self) -> None:
        t = self.topo
        seed = 0
        for p in range(t.planes):
            for s in range(t.spines):
                sw = Switch(self.sim, t.spine(p, s), p, "spine", s, seed := seed + 1)
                self.switches[sw.name] = sw
            for lf in range(t.leaves):
                sw = Switch(self.sim, t.leaf(p, lf), p, "leaf", lf, seed := seed + 1)
                self.switches[sw.name] = sw
        for n in range(t.num_hosts):
            lf, k = t.host_leaf(n)
            self.hosts.append(Host(self.sim, t.host(n), n, lf, k))
        for p in range(t.planes):
            for lf in range(t.leaves):
                leaf = self.switches[t.leaf(p, lf)]
                for s in range(t.spines):
                    spine = self.switches[t.spine(p, s)]
                    up = self._port(leaf, t.leaf_uplink(s))
                    down = self._port(spine, t.spine_port(lf))
                    connect(up, down)
                    leaf.uplinks.append(up)
                    spine.to_leaf[lf] = down
        for h in self.hosts:
            for p in range(t.planes):
                leaf = self.switches[t.leaf(p, h.leaf)]
                nic = self._port(h, t.host_nic(p))
                dl = self._port(leaf, t.leaf_downlink(h.slot))
                connect(nic, dl)
                h.nics[p] = nic
                leaf.down_to_host[h.slot] = dl

    def set_link(self, endpoint: str, up: bool, both: bool = True) -> None:
        """Bring a port (and by default its peer) up or down, like `ip link set`."""
        port = self.ports[endpoint]
        port.up = up
        if both:
            port._peer_port.up = up
