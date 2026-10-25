"""MetaRoCE endpoint parameters for the simulator. Defaults follow spec 1.0."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Config:
    mtu: int = 4096                 # payload bytes per data packet (§2.3.1)
    header_bytes: int = 32 + 16 + 4  # base + METH + RETH + mCRC, write packets
    ack_bytes: int = 32 + 32 + 4    # base + SAETH + FSN vector + mCRC
    wire_overhead: int = 14 + 40 + 8  # Ethernet + IPv6 + UDP

    # Baseline CC (§6.1, Table 6.1)
    epsilon: float = 1.0
    beta: float = 0.5
    lam: float = 1.0
    qwnd_min: int = 1
    qwnd_init_max: int = 512        # QWND_max before any RCN hint arrives
    fast_start_burst: int = 4

    # Flows (§7.3, §7.4)
    max_flows: int = 8
    average_window: int = 12        # add a flow while avg*(n+1) < QWND
    max_fwnd: int = 128

    # Reliability (§4.5, §4.8)
    # Spec ranges: lower 10 us..5 ms, upper 1..100 ms. The upper bound must
    # exceed the worst queueing delay (400 KB at 1 Gbit/s is 3.2 ms) or
    # timeouts fire spuriously and trigger failover.
    min_rto_ns: int = 100_000       # 100 us
    max_rto_ns: int = 20_000_000    # 20 ms
    rto_beta: float = 0.25          # mdev gain
    rtos_before_repath: int = 2     # LSMR default; spec: "a configurable number"
    sack_mode: str = "immediate"    # immediate | never (§4.6.1)
    ack_window_pkts: int = 4        # A-bit on every packet when FWND below this

    # RCN (§6.3)
    rcn: bool = True
    gamma: float = 1.0
    omega_ns: int = 0
    nic_gbps: float = 10.0
    sender_active_ns: int = 50_000  # a sender counts as concurrent if seen this recently

    # Multi-plane (§7.5)
    rtos_before_port_failover: int = 3   # 0 disables timeout-driven failover
    # Sender-side rate limit per QP in Gbit/s (0 = none). Models a CPU-bound
    # software transport such as libsoftmetaroce (~2 Gbit/s per QP in the lab).
    max_gbps: float = 0.0
    # A failed-over flow tries its home port again after this long (LSMR
    # restores the original pair once it is usable again).
    port_restore_ns: int = 20_000_000

    @property
    def data_wire_bytes(self) -> int:
        return self.mtu + self.header_bytes + self.wire_overhead

    @property
    def ack_wire_bytes(self) -> int:
        return self.ack_bytes + self.wire_overhead
