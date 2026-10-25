"""MetaRoCE endpoints for the simulator: sender QP, receiver QP, host binding.

The application on each sender keeps `max_msgs` RDMA Writes of `msg_size`
outstanding (like rdma_gen's posted WQEs) and posts a new one when one
completes. Packets of a message may travel on any flow; the receiver
places them as they arrive and the message completes when all arrive.
"""

from __future__ import annotations

import math
import random
import zlib

from ..engine import Sim
from ..net import CE, ECT0, Host, Packet
from .cc import QpCC
from .config import Config
from .flow import Ack, TxFlow

PORT = 2766


class Data:
    __slots__ = (
        "ack_req",
        "dqp",
        "flow",
        "fsn",
        "msn",
        "npkts",
        "posn",
        "rate",
        "retx",
        "sqp",
        "timeout",
        "tx_ts",
    )

    def __init__(self, dqp, sqp, flow, fsn, msn, posn, npkts, ack_req, retx, timeout, tx_ts,
                 rate):
        self.dqp, self.sqp, self.flow, self.fsn = dqp, sqp, flow, fsn
        self.msn, self.posn, self.npkts = msn, posn, npkts
        self.ack_req, self.retx, self.timeout, self.tx_ts, self.rate = (
            ack_req, retx, timeout, tx_ts, rate)


class AckMsg:
    __slots__ = ("ack", "dqp", "flow", "sqp")

    def __init__(self, dqp, sqp, flow, ack):
        self.dqp, self.sqp, self.flow, self.ack = dqp, sqp, flow, ack


class Frame:
    """Sender-side record of one (Flow, FSN): what it carries."""

    __slots__ = ("msn", "posn", "retx", "sent_ns")

    def __init__(self, msn, posn, sent_ns):
        self.msn, self.posn, self.sent_ns, self.retx = msn, posn, sent_ns, 0


class Stats:
    """Collects the same series the emulator's capture analysis produces."""

    def __init__(self, bin_ns: int):
        self.bin_ns = bin_ns
        self.goodput: dict = {}  # label -> {bin: bits}
        self.retx: dict = {"sack": {}, "timeout": {}}
        self.ce: dict = {}
        self.cnp: dict = {}
        self.flows: dict = {}
        self.qp_bits: dict = {}  # sender qpn -> {bin: bits}
        self.msgs_done = 0
        self.fct_ns: list = []

    def _add(self, table: dict, label: str, t: int, v: float) -> None:
        d = table.setdefault(label, {})
        b = t // self.bin_ns
        d[b] = d.get(b, 0.0) + v


class SenderQp:
    def __init__(self, sim: Sim, host: Host, cfg: Config, qpn: int, peer: Host, peer_qpn: int,
                 planes: int, msg_size: int, max_msgs: int, entropy, stats: Stats,
                 rng: random.Random, name: str):
        self.sim, self.host, self.cfg = sim, host, cfg
        self.qpn, self.peer, self.peer_qpn, self.planes = qpn, peer, peer_qpn, planes
        self.npkts = max(1, math.ceil(msg_size / cfg.mtu))
        self.max_msgs, self.entropy, self.stats, self.rng, self.name = (
            max_msgs, entropy, stats, rng, name)
        # §7.5.3: flow 0 starts at a base port hashed from the QP numbers and a
        # NIC seed; flow i goes to (base + i) mod P.
        self.base_port = zlib.crc32(f"{qpn}|{peer_qpn}|{host.name}".encode()) % planes
        self.flows = [TxFlow(flow_id=i, entropy=0, plane=(self.base_port + i) % planes)
                      for i in range(cfg.max_flows)]
        self.cc = QpCC(cfg, self.flows, entropy, ports=planes)
        self.timers: dict = {}
        self.next_msn = 0
        self.next_posn = 0
        self.inorder = False       # in-order completion (sim.completion: inorder)
        self.open_msgs: dict = {}  # msn -> [acked count, posted_ns]
        self.rr = 0
        self.est_rtt = 10_000.0
        self.running = False
        self.next_ok = 0          # pacing: earliest time the next packet may leave
        self.wake = None

    # -- application -------------------------------------------------------------

    def start(self) -> None:
        self.running = True
        self.cc.start_fast_start(self.sim.now)
        for f in self.flows:
            self._log_entropy(f)
        self.pump()

    def stop(self) -> None:
        self.running = False

    def _has_new_data(self) -> bool:
        if not self.running:
            return False
        if self.next_posn > 0:
            return True
        if self.inorder and self.open_msgs:
            # Work completions are delivered in MSN order: a message still
            # missing a packet holds its WQE slot and every one posted after it.
            return self.next_msn - next(iter(self.open_msgs)) < self.max_msgs
        return len(self.open_msgs) < self.max_msgs

    def _next_unit(self) -> tuple[int, int]:
        msn, posn = self.next_msn, self.next_posn
        if posn == 0:
            self.open_msgs[msn] = [0, self.sim.now]
        self.next_posn += 1
        if self.next_posn == self.npkts:
            self.next_msn, self.next_posn = self.next_msn + 1, 0
        return msn, posn

    # -- transmit ------------------------------------------------------------------

    def _nic_ready(self, plane: int) -> bool:
        port = self.host.nics[plane]
        return port.up and len(port.q) < 2

    def _paced(self) -> bool:
        """True if the QP rate limit holds the next packet back (and a wake is set)."""
        if not self.cfg.max_gbps or self.sim.now >= self.next_ok:
            return False
        if self.wake is None:
            self.wake = self.sim.at(self.next_ok, self._wake)
        return True

    def _wake(self) -> None:
        self.wake = None
        self.pump()

    def pump(self) -> None:
        sent = True
        while sent:
            sent = False
            if self._paced():
                return
            # retransmissions first, on any flow with a queue
            for f in self.flows:
                if f.retx_queue and self._nic_ready(f.plane):
                    fsn = f.retx_queue.pop(0)
                    fr = f.inflight.get(fsn)
                    if fr is not None:
                        self._send(f, fsn, fr, retx=True)
                        sent = True
            if not self._has_new_data():
                continue
            if self.cc.fast_start:
                f = self.cc.fast_start_flow_for_send()
                if f is not None and self._nic_ready(f.plane):
                    self._send_new(f)
                    sent = True
                    continue
                if f is not None:  # undo the window credit we could not use
                    f.fwnd -= 1
                    self.cc.qwnd -= 1
                    continue
            act = [f for f in self.flows if f.active]
            for i in range(len(act)):
                f = act[(self.rr + i) % len(act)]
                if f.room() > 0 and self._nic_ready(f.plane):
                    self.rr = (self.rr + i + 1) % len(act)
                    self._send_new(f)
                    sent = True
                    break

    def _send_new(self, f: TxFlow) -> None:
        msn, posn = self._next_unit()
        fr = Frame(msn, posn, self.sim.now)
        fsn = f.take_fsn(fr)
        self._send(f, fsn, fr, retx=False)

    def _send(self, f: TxFlow, fsn: int, fr: Frame, retx: bool, timeout: bool = False) -> None:
        now = self.sim.now
        fr.sent_ns = now
        self._log_entropy(f)
        if retx:
            fr.retx += 1
            self.stats._add(self.stats.retx, "timeout" if timeout else "sack", now, 1)
            self.stats.flows[self._fkey(f)]["retx"] += 1
            self.stats.flows[self._fkey(f)]["rto"] += timeout
        last_of_window = f.room() <= 0
        ack_req = (retx or last_of_window or f.fwnd < self.cfg.ack_window_pkts
                   or (fsn % 4 == 3) or not self._has_new_data())
        rate = int(self.cc.qwnd * self.cfg.mtu * 8 / max(self.est_rtt, 1.0))
        body = Data(self.peer_qpn, self.qpn, f.flow_id, fsn, fr.msn, fr.posn, self.npkts,
                    ack_req, retx, timeout, now, rate)
        pkt = Packet(self.host.addr(f.plane), self.peer.addr(f.plane), f.entropy, PORT,
                     self.cfg.data_wire_bytes, ECT0, body)
        st = self.stats.flows[self._fkey(f)]
        st["pkts"] += 1
        if not st["sports"] or st["sports"][-1][1] != f.entropy:
            st["sports"].append([now / 1e9, f.entropy])
        st["plane"] = f.plane
        self.host.send(pkt, f.plane)
        if self.cfg.max_gbps:
            self.next_ok = now + int(pkt.size * 8 / self.cfg.max_gbps)
        self._arm(f)

    # -- timers ----------------------------------------------------------------

    def _arm(self, f: TxFlow) -> None:
        old = self.timers.get(f.flow_id)
        if old is not None:
            Sim.cancel(old)
        if f.inflight:
            rto = f.rto_ns(self.cfg.min_rto_ns, self.cfg.max_rto_ns)
            self.timers[f.flow_id] = self.sim.after(rto, self._on_rto, f)
        else:
            self.timers.pop(f.flow_id, None)

    def _on_rto(self, f: TxFlow) -> None:
        self.timers.pop(f.flow_id, None)
        if not f.inflight:
            return
        if f.retx_queue:
            # LSMR (TxFlow::prepareRetxPacketsIfAny): the previous timeout's
            # retransmissions are still queued, so re-queueing the window would
            # send it twice. Count the timeout, re-arm, and let them drain.
            f.rtos_without_ack += 1
            self._arm(f)
            self.pump()
            return
        resend = f.on_rto()
        if f.rtos_without_ack >= self.cfg.rtos_before_repath:
            f.entropy = self.entropy()  # re-steer: same flow, new path (§4.8)
            self._log_entropy(f)
        if (self.planes > 1 and self.cfg.rtos_before_port_failover
                and f.rtos_without_ack >= self.cfg.rtos_before_port_failover):
            others = [p for p in range(self.planes)
                      if p != f.plane and self.host.nics[p].up]
            if others:
                f.plane = self.rng.choice(others)  # random among usable ports (§7.5.4)
                f.rtos_without_ack = 0
                self.sim.after(self.cfg.port_restore_ns, self._restore, f)
        self.cc.on_ack(f, acked=0, ce=0, lost=len(resend), window_limited=False)
        for fsn in resend[:max(1, f.fwnd)]:
            fr = f.inflight.get(fsn)
            if fr is not None and self._nic_ready(f.plane):
                self._send(f, fsn, fr, retx=True, timeout=True)
            elif fr is not None:
                f.retx_queue.append(fsn)
        self._arm(f)
        self.pump()

    def _restore(self, f: TxFlow) -> None:
        home = (self.base_port + f.flow_id) % self.planes
        if f.plane != home and self.host.nics[home].up:
            f.plane = home
            f.entropy = self.entropy()
            self.pump()

    # -- acknowledgments -----------------------------------------------------------

    def on_ack(self, a: AckMsg) -> None:
        f = self.flows[a.flow]
        was_limited = f.room() <= 0
        newly, resend = f.on_ack(a.ack, self.cfg.sack_mode)
        if a.ack.echo_ts is not None:
            rtt = self.sim.now - a.ack.echo_ts
            f.rtt_sample(rtt, self.cfg.rto_beta)
            self.est_rtt = f.srtt_ns
        if a.ack.rate_gbps:
            self.cc.on_rate_hint(a.ack.rate_gbps, self.est_rtt)
        for fr in f.last_frames:
            self.frame_delivered(fr.msn)
        self.cc.maybe_exit_fast_start(self.sim.now, a.ack.cnp > 0, self._has_new_data(),
                                      self.est_rtt)
        if not self.cc.fast_start:
            self.cc.on_ack(f, acked=len(newly), ce=a.ack.cnp, lost=len(resend),
                           window_limited=was_limited)
        f.retx_queue.extend(resend)
        self._arm(f)
        self.pump()

    def frame_delivered(self, msn: int) -> None:
        m = self.open_msgs.get(msn)
        if m is None:
            return
        m[0] += 1
        if m[0] >= self.npkts:
            del self.open_msgs[msn]
            self.stats.msgs_done += 1
            self.stats.fct_ns.append(self.sim.now - m[1])

    def _fkey(self, f: TxFlow) -> str:
        return f"{self.name}/flow{f.flow_id}"

    def _log_entropy(self, f: TxFlow) -> None:
        self.stats.flows.setdefault(self._fkey(f), {"plane": f.plane, "pkts": 0, "retx": 0,
                                                    "rto": 0, "sports": []})


class ReceiverQp:
    def __init__(self, sim: Sim, host: Host, cfg: Config, qpn: int, stats: Stats, rcn):
        self.sim, self.host, self.cfg, self.qpn, self.stats, self.rcn = (
            sim, host, cfg, qpn, stats, rcn)
        self.rx: dict = {}     # flow -> [cum_fsn, set beyond, max_fsn, cnp]
        self.msgs: dict = {}   # msn -> set of posns received

    def on_data(self, pkt: Packet, plane: int) -> None:
        d: Data = pkt.body
        st = self.rx.setdefault(d.flow, [-1, set(), -1, 0])
        cum, beyond = st[0], st[1]
        fresh = d.fsn > cum and d.fsn not in beyond
        if pkt.ecn == CE:
            st[3] += 1
            self.stats._add(self.stats.ce, f"plane {plane}", self.sim.now, 1)
        if fresh:
            beyond.add(d.fsn)
            while cum + 1 in beyond:
                cum += 1
                beyond.discard(cum)
            st[0] = cum
            st[2] = max(st[2], d.fsn)
            got = self.msgs.setdefault(d.msn, set())
            if d.posn not in got:
                got.add(d.posn)
                self.stats._add(self.stats.goodput, f"plane {plane}", self.sim.now,
                                pkt.size * 8)
                self.stats._add(self.stats.qp_bits, d.sqp, self.sim.now, pkt.size * 8)
                if len(got) >= d.npkts:
                    del self.msgs[d.msn]
        self.rcn.seen(d.sqp, d.rate, self.sim.now)
        if d.ack_req or not fresh or beyond or st[3] >= 15:
            self._ack(pkt, plane, st, d)

    def _ack(self, pkt: Packet, plane: int, st, d: Data) -> None:
        cnp, st[3] = st[3], 0
        if cnp:
            self.stats._add(self.stats.cnp, f"plane {plane}", self.sim.now, cnp)
        cum = st[0]
        sacked = frozenset(f for f in st[1] if f <= cum + 256)  # 256-bit vector (§8.5.9)
        ack = Ack(cum_fsn=cum, max_fsn=max(st[2], cum), sacked=sacked,
                  cnp=min(cnp, 15), rate_gbps=self.rcn.target(), echo_ts=d.tx_ts)
        back = Packet(pkt.dst, pkt.src, pkt.sport, PORT, self.cfg.ack_wire_bytes, ECT0,
                      AckMsg(d.sqp, self.qpn, d.flow, ack))
        self.host.send(back, plane)


class Rcn:
    """Receiver-side target rate: link rate over estimated concurrent senders (§6.3.1)."""

    def __init__(self, cfg: Config, host: Host):
        self.cfg, self.host = cfg, host
        self.last: dict = {}

    def seen(self, sqp: int, rate: int, now: int) -> None:
        if rate:
            self.last[sqp] = now
        self.now = now

    def target(self) -> int:
        if not self.cfg.rcn:
            return 0
        now = getattr(self, "now", 0)
        n = sum(1 for t in self.last.values() if now - t <= self.cfg.sender_active_ns) or 1
        ports_up = sum(1 for p in self.host.nics.values() if p.up) or 1
        total = self.cfg.nic_gbps * ports_up
        return max(1, math.ceil(total / n))


class Endpoint:
    """MetaRoCE on one simulated host: dispatches UDP 2766 to its QPs."""

    def __init__(self, sim: Sim, host: Host, cfg: Config):
        self.sim, self.host, self.cfg = sim, host, cfg
        self.senders: dict = {}
        self.receivers: dict = {}
        self.rcn = Rcn(cfg, host)
        host.handlers[PORT] = self.dispatch
        for port in host.nics.values():
            port.on_drain = self._drain

    def _drain(self) -> None:
        for s in self.senders.values():
            s.pump()

    def dispatch(self, pkt: Packet, plane: int) -> None:
        body = pkt.body
        if isinstance(body, AckMsg):
            s = self.senders.get(body.dqp)
            if s is not None:
                s.on_ack(body)
        else:
            r = self.receivers.get(body.dqp)
            if r is not None:
                r.on_data(pkt, plane)
