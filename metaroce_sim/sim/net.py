"""Network elements: packets, ports with RED/ECN queues, switches, hosts.

Addresses are (plane, leaf, slot) tuples, matching the emulator's
`fd00:<plane>:<leaf>:<slot>::2` host addresses.
"""

from __future__ import annotations

import random
import zlib
from dataclasses import dataclass, field

from .engine import Sim

NOT_ECT, ECT0, CE = 0, 2, 3


@dataclass
class Packet:
    src: tuple
    dst: tuple
    sport: int
    dport: int
    size: int  # bytes on the wire
    ecn: int = ECT0
    body: object = None
    sent_ns: int = 0
    hops: list = field(default_factory=list)
    via: int | None = None   # source-routed spine (SRv6 uSID path, MRC); None = ECMP hash
    trimmed: bool = False    # payload cut by a trimming switch (MRC)


@dataclass
class Red:
    """RED with ECN marking (like `tc qdisc red ... ecn`), in bytes."""

    min_th: int
    max_th: int
    max_p: float
    limit: int

    def mark_prob(self, qbytes: int) -> float:
        if qbytes <= self.min_th:
            return 0.0
        if qbytes >= self.max_th:
            return 1.0
        return self.max_p * (qbytes - self.min_th) / (self.max_th - self.min_th)


class Port:
    """Egress queue + serializer + link to a peer node."""

    def __init__(self, sim: Sim, owner: Node, name: str, rate_bps: float, prop_ns: int,
                 red: Red | None = None, limit_bytes: int = 1 << 20, rng=None,
                 trim_bytes: int | None = None):
        self.sim, self.owner, self.name = sim, owner, name
        self.rate_bps, self.prop_ns = rate_bps, prop_ns
        self.red, self.limit = red, (red.limit if red else limit_bytes)
        self.peer: Node | None = None
        self.up = True
        self.q: list[Packet] = []
        self.qbytes = 0
        self.busy = False
        self.rng = rng or random.Random(zlib.crc32(name.encode()))
        self.trim_bytes = trim_bytes  # trim instead of tail-drop; header size kept
        self.loss = 0.0          # netem-style random loss on egress
        self.on_drain = None     # callable fired when the queue empties (NIC backpressure)
        self.stats = {"tx": 0, "tx_bytes": 0, "drop_q": 0, "drop_down": 0, "ce": 0,
                      "drop_loss": 0}

    def ser_ns(self, size: int) -> int:
        return max(1, round(size * 8 * 1e9 / self.rate_bps))

    def send(self, pkt: Packet) -> None:
        if not self.up:
            self.stats["drop_down"] += 1
            return
        if self.loss and self.rng.random() < self.loss:
            self.stats["drop_loss"] += 1
            return
        if self.qbytes + pkt.size > self.limit:
            if self.trim_bytes is None or pkt.trimmed:
                self.stats["drop_q"] += 1
                return
            # Packet trimming: forward only the headers, ahead of queued data, so
            # the receiver learns of the congestion loss within about one RTT.
            pkt.trimmed = True
            pkt.size = self.trim_bytes
            self.stats["trim"] = self.stats.get("trim", 0) + 1
            pos = 1 if self.busy else 0
            self.q.insert(pos, pkt)
            self.qbytes += pkt.size
            if not self.busy:
                self._start()
            return
        marked = (
            self.red is not None and pkt.ecn not in (NOT_ECT, CE)
            and self.rng.random() < self.red.mark_prob(self.qbytes)
        )
        if marked:
            self.stats["ce"] += 1
            pkt.ecn = CE
        self.q.append(pkt)
        self.qbytes += pkt.size
        if not self.busy:
            self._start()

    def _start(self) -> None:
        pkt = self.q[0]
        self.busy = True
        self.sim.after(self.ser_ns(pkt.size), self._done)

    def _done(self) -> None:
        pkt = self.q.pop(0)
        self.qbytes -= pkt.size
        self.stats["tx"] += 1
        self.stats["tx_bytes"] += pkt.size
        if self.up and self.peer is not None:
            self.sim.after(self.prop_ns, self.peer.receive, pkt, self)
        else:
            self.stats["drop_down"] += 1
        if self.q:
            self._start()
        else:
            self.busy = False
            if self.on_drain is not None:
                self.on_drain()


class Node:
    def __init__(self, sim: Sim, name: str):
        self.sim, self.name = sim, name
        self.ports: dict[str, Port] = {}

    def receive(self, pkt: Packet, via: Port) -> None:  # pragma: no cover - abstract
        raise NotImplementedError


def connect(a: Port, b: Port) -> None:
    a.peer, b.peer = b.owner, a.owner
    a._peer_port, b._peer_port = b, a


def ecmp_hash(seed: int, pkt: Packet) -> int:
    key = f"{seed}|{pkt.src}|{pkt.dst}|{pkt.sport}|{pkt.dport}|17".encode()
    return zlib.crc32(key)


class Switch(Node):
    """Two-tier Clos switch. `route(pkt)` returns candidate egress ports."""

    def __init__(self, sim: Sim, name: str, plane: int, role: str, index: int, seed: int):
        super().__init__(sim, name)
        self.plane, self.role, self.index, self.seed = plane, role, index, seed
        self.down_to_host: dict[int, Port] = {}  # leaf: slot -> port
        self.uplinks: list[Port] = []  # leaf: spine order
        self.to_leaf: dict[int, Port] = {}  # spine: leaf -> port
        self.drops_no_route = 0

    def receive(self, pkt: Packet, via: Port) -> None:
        pkt.hops.append(self.name)
        _plane, leaf, slot = pkt.dst
        if self.role == "leaf":
            if leaf == self.index:
                out = self.down_to_host.get(slot)
            elif pkt.via is not None:
                # Source routed (static SRv6 uSID): no local repair; a down
                # uplink drops the packet, as with MRC's static routes.
                out = self.uplinks[pkt.via] if pkt.via < len(self.uplinks) else None
            else:
                live = [u for u in self.uplinks if u.up]  # local link-down drops the next hop
                out = live[ecmp_hash(self.seed, pkt) % len(live)] if live else None
        else:
            out = self.to_leaf.get(leaf)
        if out is None:
            self.drops_no_route += 1
            return
        out.send(pkt)


class Host(Node):
    """A host with one NIC per plane. Delivers packets to `handler(pkt, plane)`."""

    def __init__(self, sim: Sim, name: str, number: int, leaf: int, slot: int):
        super().__init__(sim, name)
        self.number, self.leaf, self.slot = number, leaf, slot
        self.nics: dict[int, Port] = {}
        self.handlers: dict[int, object] = {}  # dport -> callable(pkt, plane)

    def addr(self, plane: int) -> tuple:
        return (plane, self.leaf, self.slot)

    def send(self, pkt: Packet, plane: int) -> None:
        pkt.sent_ns = self.sim.now
        self.nics[plane].send(pkt)

    def receive(self, pkt: Packet, via: Port) -> None:
        h = self.handlers.get(pkt.dport)
        if h is not None:
            h(pkt, pkt.dst[0])
