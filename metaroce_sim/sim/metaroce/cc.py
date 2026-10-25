"""Hierarchical congestion control for one QP (spec §6 and §7.4).

QWND is fractional; each active flow holds an integer FWND, and the
invariant floor(QWND) == sum(FWND) is restored after every change by
moving whole packets of window between flows:

- growth goes to the flow that was window limited (or a flow being
  bootstrapped after activation),
- cuts come from the flow that saw the congestion, which shifts load
  toward uncongested paths (§6.1.2).
"""

from __future__ import annotations

import math

from .config import Config
from .flow import TxFlow


class QpCC:
    def __init__(self, cfg: Config, flows: list[TxFlow], new_entropy, ports: int = 1):
        self.cfg = cfg
        # Flows 0..ports-1 sit on distinct ports (§7.5.3); fast start must begin
        # a flow on every available port (§6.1.3), and those stay active.
        self.ports = max(1, ports)
        self.flows = flows                  # all configured flows, index = flow_id
        self.new_entropy = new_entropy      # callable -> next entropy value
        self.qwnd = 0.0
        self.qwnd_max = float(cfg.qwnd_init_max)
        self.fast_start = False
        self.fast_start_flow = 0
        self.fast_start_t0 = 0
        self.bootstrapping: int | None = None  # flow being grown to fast_start_burst

    # -- helpers -----------------------------------------------------------------

    def active(self) -> list[TxFlow]:
        return [f for f in self.flows if f.active]

    def _activate(self, f: TxFlow) -> None:
        f.active = True
        f.entropy = self.new_entropy()   # a claimed flow gets a new entropy (§7.4.2)

    def _sum(self) -> int:
        return sum(f.fwnd for f in self.flows if f.active)

    def _rebalance(self, grow: TxFlow | None, shrink: TxFlow | None) -> None:
        target = max(self.cfg.qwnd_min, math.floor(self.qwnd))
        act = self.active()
        while self._sum() < target:
            pick = None
            if self.bootstrapping is not None and \
                    self.flows[self.bootstrapping].fwnd < self.cfg.fast_start_burst:
                pick = self.flows[self.bootstrapping]
            elif grow is not None and grow.active and grow.fwnd < self.cfg.max_fwnd:
                pick = grow
            else:
                cands = [f for f in act if f.fwnd < self.cfg.max_fwnd]
                pick = min(cands, key=lambda f: f.fwnd) if cands else None
            if pick is None:
                break
            pick.fwnd += 1
        while self._sum() > target:
            cands = [f for f in act if f.fwnd > 0]
            if not cands:
                break
            pick = shrink if shrink in cands else max(cands, key=lambda f: f.fwnd)
            pick.fwnd -= 1
            if pick.fwnd == 0 and pick.outstanding() == 0 and pick.flow_id >= self.ports:
                pick.active = False  # empty window -> inactive (§7.4.1)
        if self.bootstrapping is not None and \
                self.flows[self.bootstrapping].fwnd >= self.cfg.fast_start_burst:
            self.bootstrapping = None
        self._maybe_add_flow()

    def _maybe_add_flow(self) -> None:
        if self.bootstrapping is not None:
            return
        act = self.active()
        if len(act) >= self.cfg.max_flows:
            return
        if self.cfg.average_window * (len(act) + 1) < self.qwnd:
            spare = [f for f in self.flows if not f.active]
            if spare:
                f = min(spare, key=lambda f: f.flow_id)  # lowest available (§7.4.2)
                self._activate(f)
                self.bootstrapping = f.flow_id

    # -- fast start (§6.1.3) -----------------------------------------------------

    def start_fast_start(self, now: int) -> None:
        self.fast_start = True
        self.fast_start_t0 = now
        self.fast_start_flow = 0
        for f in self.flows:
            f.fwnd = 0
        self.qwnd = 0.0
        for f in self.flows[:self.ports]:  # one flow on every port
            if not f.active:
                self._activate(f)

    def fast_start_flow_for_send(self) -> TxFlow | None:
        """Flow to put the next fast-start packet on; grows windows as it sends."""
        if self.qwnd >= self.qwnd_max:
            self.fast_start = False
            return None
        # First packet on every port's flow, then fast_start_burst per flow.
        unvisited = [g for g in self.flows[:self.ports] if g.fwnd == 0]
        if unvisited:
            f = unvisited[0]
            f.fwnd += 1
            self.qwnd += 1
            return f
        f = self.flows[self.fast_start_flow]
        if f.fwnd >= self.cfg.fast_start_burst and self.fast_start_flow + 1 < len(self.flows):
            self.fast_start_flow += 1
            f = self.flows[self.fast_start_flow]
            if not f.active:
                self._activate(f)
        f.fwnd += 1
        self.qwnd += 1
        return f

    def maybe_exit_fast_start(self, now: int, ack_ce: bool, pending: bool, est_rtt: float) -> None:
        if not self.fast_start:
            return
        if (pending or ack_ce or self.qwnd >= self.qwnd_max
                or (pending and now - self.fast_start_t0 > est_rtt)):
            self.fast_start = False
            for f in self.flows:
                if f.active and f.fwnd == 0 and f.outstanding() == 0 \
                        and f.flow_id >= self.ports:
                    f.active = False
            self.qwnd = max(self.qwnd, float(self._sum()))

    # -- ACK reaction (§6.1.2, §6.3.3) -------------------------------------------

    def on_ack(self, flow: TxFlow, acked: int, ce: int, lost: int, window_limited: bool) -> None:
        cfg = self.cfg
        clean = max(0, acked - ce)
        if window_limited and self.qwnd < self.qwnd_max:
            for _ in range(clean):
                if self.qwnd < cfg.epsilon:
                    self.qwnd += 1  # slow-start-like growth while QWND < epsilon
                else:
                    self.qwnd = min(self.qwnd + min(cfg.epsilon, self.qwnd) / self.qwnd,
                                    self.qwnd_max)
        if ce:
            self.qwnd = max(self.qwnd - cfg.beta * ce, cfg.qwnd_min)
        if lost:
            self.qwnd = max(self.qwnd - cfg.lam * lost, cfg.qwnd_min)
        # RCN reduction zones: over the cap, shed window per ACKed packet.
        if self.qwnd > self.qwnd_max:
            per = 1.0 if self.qwnd > 2 * self.qwnd_max else 0.5
            self.qwnd = max(self.qwnd - per * max(acked, 1), self.qwnd_max, cfg.qwnd_min)
        self._rebalance(grow=flow if window_limited else None,
                        shrink=flow if (ce or lost) else None)
        if window_limited and clean and not ce and not lost:
            self._equalize(flow)

    def _equalize(self, flow: TxFlow) -> None:
        """Move one packet of window to a window-limited flow from the largest flow.

        Growth stops once QWND reaches QWND_max, so without this a flow that
        was cut once (e.g. by an early loss) would stay small for good. Taking
        from the largest flow on clean ACKs lets window drift back toward
        flows whose paths are working, in the spirit of §6.1.2's shift of
        window from congested to uncongested flows.
        """
        act = [f for f in self.flows if f.active]
        biggest = max(act, key=lambda f: f.fwnd)
        if biggest is not flow and biggest.fwnd > flow.fwnd + 1:
            biggest.fwnd -= 1
            flow.fwnd += 1

    def on_rate_hint(self, gbps: int, rtt_ns: float) -> None:
        """Receiver target rate -> QWND_max (§6.3.2), in packets."""
        if not gbps or not self.cfg.rcn:
            return
        bdp_bits = self.cfg.gamma * gbps * (rtt_ns + self.cfg.omega_ns)  # Gbit/s * ns = bits
        self.qwnd_max = max(float(self.cfg.qwnd_min), bdp_bits / (8 * self.cfg.mtu))
