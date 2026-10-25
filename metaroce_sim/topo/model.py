"""Multi-plane Clos topology model.

Each plane is an independent two-tier Clos: every leaf connects to every
spine. Every host has one NIC per plane, attached to the leaf with the
same index in each plane (spec §7.5 "multi-plane" wiring).

Addressing (IPv6, all /64 links, values in hex fields):

    host NIC, plane p, leaf l, host k:  fd00:p:l:k::2   (gateway ::1 on the leaf)
    leaf l <-> spine s link, plane p:   fc00:p:s:l::1 (leaf), ::2 (spine)
    host prefix routed per leaf:        fd00:p:l::/48
    plane prefix on a host NIC:         fd00:p::/32
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class LinkSpec:
    mtu: int = 4200
    rate: str | None = None  # e.g. "1gbit"; None = unshaped veth
    delay: str | None = None  # e.g. "10us"; applied with netem


@dataclass(frozen=True)
class Topology:
    name: str
    planes: int
    spines: int  # per plane
    leaves: int  # per plane
    hosts_per_leaf: int
    link: LinkSpec = field(default_factory=LinkSpec)
    host_image: str = "metaroce-host:latest"
    switch_image: str = "metaroce-switch:latest"

    def __post_init__(self):
        for attr in ("planes", "spines", "leaves", "hosts_per_leaf"):
            v = getattr(self, attr)
            if not 1 <= v <= 0xFF:
                raise ValueError(f"{attr} must be 1..255, got {v}")
        if self.planes > 16:
            raise ValueError("MetaRoCE signals at most 16 ports/planes (spec §7.5)")

    @property
    def num_hosts(self) -> int:
        return self.leaves * self.hosts_per_leaf

    # -- names ---------------------------------------------------------------

    @staticmethod
    def spine(p: int, s: int) -> str:
        return f"p{p}-spine{s}"

    @staticmethod
    def leaf(p: int, lf: int) -> str:
        return f"p{p}-leaf{lf}"

    @staticmethod
    def host(n: int) -> str:
        return f"h{n:02d}"

    def host_leaf(self, n: int) -> tuple[int, int]:
        """(leaf index, host index on that leaf) for host number n."""
        return divmod(n, self.hosts_per_leaf)

    # -- interfaces (containerlab linux nodes: eth0 is management) -----------

    @staticmethod
    def leaf_uplink(s: int) -> str:
        return f"eth{1 + s}"

    def leaf_downlink(self, k: int) -> str:
        return f"eth{1 + self.spines + k}"

    @staticmethod
    def spine_port(lf: int) -> str:
        return f"eth{1 + lf}"

    @staticmethod
    def host_nic(p: int) -> str:
        return f"eth{1 + p}"

    # -- addresses -------------------------------------------------------------

    def host_addr(self, n: int, p: int) -> str:
        lf, k = self.host_leaf(n)
        return f"fd00:{p:x}:{lf:x}:{k:x}::2"

    def host_gw(self, n: int, p: int) -> str:
        lf, k = self.host_leaf(n)
        return f"fd00:{p:x}:{lf:x}:{k:x}::1"

    @staticmethod
    def leaf_prefix(p: int, lf: int) -> str:
        return f"fd00:{p:x}:{lf:x}::/48"

    @staticmethod
    def plane_prefix(p: int) -> str:
        return f"fd00:{p:x}::/32"

    @staticmethod
    def fabric_addr(p: int, s: int, lf: int, side: str) -> str:
        return f"fc00:{p:x}:{s:x}:{lf:x}::{1 if side == 'leaf' else 2}"

    # -- wiring ----------------------------------------------------------------

    def links(self) -> list[tuple[str, str]]:
        """containerlab endpoint pairs, "node:iface"."""
        out = []
        for p in range(self.planes):
            for lf in range(self.leaves):
                for s in range(self.spines):
                    out.append((f"{self.leaf(p, lf)}:{self.leaf_uplink(s)}",
                                f"{self.spine(p, s)}:{self.spine_port(lf)}"))
        for n in range(self.num_hosts):
            lf, k = self.host_leaf(n)
            for p in range(self.planes):
                out.append((f"{self.host(n)}:{self.host_nic(p)}",
                            f"{self.leaf(p, lf)}:{self.leaf_downlink(k)}"))
        return out


def load(path: str | Path) -> Topology:
    d = yaml.safe_load(Path(path).read_text())
    link = LinkSpec(**(d.pop("link", None) or {}))
    return Topology(link=link, **d)
