"""A minimal MRC model for head-to-head runs with MetaRoCE.

Follows the MRC + SRv6 paper's mechanisms at plane/spine granularity:

- One QP, one packet sequence space. Every packet is source routed on an
  entropy value (EV) = (plane, spine), rotated per packet across the EVs
  that are usable (SRv6 uSID paths, no ECMP hashing).
- Selective ACKs per QP. A trimmed packet (header only) is NACKed at once
  and resent on a different EV; that is a congestion signal, not a path
  failure, so the EV keeps its state.
- Loss without trimming (found by timeout) marks the EV BAD: it is
  retired and background probes bring it back when it answers.
- ECN echoed per EV makes the sender avoid that EV for a while (load
  balancing), as long as other EVs remain.
- Window control is the same baseline AIMD as the MetaRoCE model (per
  ECN mark, per loss), on one QP window, so a comparison isolates path
  control rather than congestion control.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..engine import Sim
from ..metaroce.endpoint import Stats
from ..net import CE, ECT0, Host, Packet

PORT = 4791  # RoCEv2 port; MRC extends RoCEv2 RC


@dataclass
class MrcConfig:
    mtu: int = 4096
    data_wire_bytes: int = 4096 + 28 + 16 + 4 + 62  # BTH-ish + RETH + ICRC + L2/L3/L4
    ack_wire_bytes: int = 128
    trim_bytes: int = 128          # trimmed data packet (headers only)
    init_qwnd: int = 16
    max_qwnd: int = 1024
    epsilon: float = 1.0
    beta: float = 0.5
    lam: float = 1.0
    min_rto_ns: int = 100_000
    max_rto_ns: int = 20_000_000
    avoid_ns: int = 200_000        # ECN: steer away from the EV this long
    probe_ns: int = 1_000_000      # probe interval for BAD EVs
    ack_every: int = 4


GOOD, BAD = "good", "bad"


class Ev:
    __slots__ = ("avoid_until", "bad_since", "plane", "retx", "rto", "sent", "spine", "state")

    def __init__(self, plane, spine):
        self.plane, self.spine = plane, spine
        self.state, self.avoid_until = GOOD, 0
        self.sent = self.retx = self.rto = 0
        self.bad_since = None


class MData:
    __slots__ = ("dqp", "ev", "msn", "npkts", "posn", "probe", "psn", "sqp", "tx_ts")

    def __init__(self, dqp, sqp, psn, msn, posn, npkts, ev, tx_ts, probe=False):
        self.dqp, self.sqp, self.psn, self.msn, self.posn = dqp, sqp, psn, msn, posn
        self.npkts, self.ev, self.tx_ts, self.probe = npkts, ev, tx_ts, probe


class MAck:
    __slots__ = ("ce_evs", "cum", "dqp", "echo_ts", "nack", "probe_ev", "sacked")

    def __init__(self, dqp, cum, sacked, nack, ce_evs, echo_ts, probe_ev=None):
        self.dqp, self.cum, self.sacked, self.nack = dqp, cum, sacked, nack
        self.ce_evs, self.echo_ts, self.probe_ev = ce_evs, echo_ts, probe_ev


class MrcSender:
    def __init__(self, sim: Sim, host: Host, peer: Host, cfg: MrcConfig, qpn: int, peer_qpn: int,
                 planes: int, spines: int, msg_size: int, max_msgs: int, stats: Stats,
                 name: str):
        self.sim, self.host, self.peer, self.cfg = sim, host, peer, cfg
        self.qpn, self.peer_qpn, self.stats, self.name = qpn, peer_qpn, stats, name
        self.evs = [Ev(p, s) for s in range(spines) for p in range(planes)]  # planes interleaved
        self.npkts = max(1, math.ceil(msg_size / cfg.mtu))
        self.max_msgs = max_msgs
        self.qwnd = float(cfg.init_qwnd)
        self.psn = 0
        self.inflight: dict = {}  # psn -> [ev index, sent_ns, msn, posn, retx]
        self.retx_q: list = []
        self.next_msn = self.next_posn = 0
        self.inorder = False       # in-order completion (sim.completion: inorder)
        self.open_msgs: dict = {}
        self.rr = 0
        self.srtt = None
        self.mdev = 0.0
        self.timer = None
        self.running = False
        for i, ev in enumerate(self.evs):
            stats.flows[self._key(i)] = {"plane": ev.plane, "pkts": 0, "retx": 0, "rto": 0,
                                         "sports": [[0.0, ev.spine]]}

    def _key(self, i: int) -> str:
        ev = self.evs[i]
        return f"{self.name}/ev{ev.plane}.{ev.spine}"

    # -- application ---------------------------------------------------------------

    def start(self) -> None:
        self.running = True
        self.sim.after(self.cfg.probe_ns, self._probe_tick)
        self.pump()

    def stop(self) -> None:
        self.running = False

    def _has_new(self) -> bool:
        if not self.running:
            return False
        if self.next_posn > 0:
            return True
        if self.inorder and self.open_msgs:
            # Work completions are delivered in MSN order: a message still
            # missing a packet holds its WQE slot and every one posted after it.
            return self.next_msn - next(iter(self.open_msgs)) < self.max_msgs
        return len(self.open_msgs) < self.max_msgs

    def _next_unit(self):
        msn, posn = self.next_msn, self.next_posn
        if posn == 0:
            self.open_msgs[msn] = [0, self.sim.now]
        self.next_posn += 1
        if self.next_posn == self.npkts:
            self.next_msn, self.next_posn = self.next_msn + 1, 0
        return msn, posn

    # -- EV choice -------------------------------------------------------------------

    def _pick_ev(self, avoid: int | None = None) -> int | None:
        now = self.sim.now
        good = [i for i, e in enumerate(self.evs) if e.state == GOOD and i != avoid
                and self.host.nics[e.plane].up]
        if not good:
            good = [i for i, e in enumerate(self.evs) if e.state == GOOD]
        if not good:
            return None
        cool = [i for i in good if self.evs[i].avoid_until <= now] or good
        for k in range(len(self.evs)):
            i = (self.rr + k) % len(self.evs)
            if i in cool:
                self.rr = i + 1
                return i
        return cool[0]

    # -- transmit ----------------------------------------------------------------------

    def _nic_ready(self, plane: int) -> bool:
        return len(self.host.nics[plane].q) < 2

    def pump(self) -> None:
        while True:
            if self.retx_q:
                psn, avoid = self.retx_q[0]
                rec = self.inflight.get(psn)
                if rec is None:
                    self.retx_q.pop(0)
                    continue
                i = self._pick_ev(avoid)
                if i is None or not self._nic_ready(self.evs[i].plane):
                    return
                self.retx_q.pop(0)
                self._send(psn, rec, i, retx=True)
                continue
            if not self._has_new() or len(self.inflight) >= int(self.qwnd):
                return
            i = self._pick_ev()
            if i is None or not self._nic_ready(self.evs[i].plane):
                return
            msn, posn = self._next_unit()
            psn = self.psn
            self.psn += 1
            rec = [i, 0, msn, posn, 0]
            self.inflight[psn] = rec
            self._send(psn, rec, i, retx=False)

    def _send(self, psn: int, rec: list, i: int, retx: bool, timeout: bool = False) -> None:
        ev = self.evs[i]
        rec[0], rec[1] = i, self.sim.now
        st = self.stats.flows[self._key(i)]
        st["pkts"] += 1
        if retx:
            rec[4] += 1
            st["retx"] += 1
            st["rto"] += timeout
            self.stats._add(self.stats.retx, "timeout" if timeout else "nack", self.sim.now, 1)
        body = MData(self.peer_qpn, self.qpn, psn, rec[2], rec[3], self.npkts, i, self.sim.now)
        pkt = Packet(self.host.addr(ev.plane), self.peer.addr(ev.plane), 49152 + i, PORT,
                     self.cfg.data_wire_bytes, ECT0, body, via=ev.spine)
        self.host.send(pkt, ev.plane)
        self._arm()

    # -- timers / probes -----------------------------------------------------------------

    def _rto(self) -> int:
        base = self.cfg.max_rto_ns if self.srtt is None else self.srtt + 4 * self.mdev
        return int(min(self.cfg.max_rto_ns, max(self.cfg.min_rto_ns, base)))

    def _arm(self) -> None:
        if self.timer is not None:
            Sim.cancel(self.timer)
        self.timer = self.sim.after(self._rto(), self._on_timeout) if self.inflight else None

    def _on_timeout(self) -> None:
        self.timer = None
        now, rto = self.sim.now, self._rto()
        lost = [psn for psn, r in self.inflight.items() if now - r[1] >= rto]
        for psn in lost:
            i = self.inflight[psn][0]
            ev = self.evs[i]
            if ev.state == GOOD:  # untrimmed loss: assume the path failed, retire it
                ev.state, ev.bad_since = BAD, now
                self.stats.flows[self._key(i)]["sports"].append([now / 1e9, -1])
            self.inflight[psn][1] = now
            self._resend(psn, i, timeout=True)
        if lost:
            self.qwnd = max(1.0, self.qwnd - self.cfg.lam * len(lost))
        self._arm()
        self.pump()

    def _resend(self, psn: int, avoid: int, timeout: bool) -> None:
        i = self._pick_ev(avoid)
        rec = self.inflight.get(psn)
        if rec is None:
            return
        if i is not None and self._nic_ready(self.evs[i].plane):
            self._send(psn, rec, i, retx=True, timeout=timeout)
        else:
            self.retx_q.append((psn, avoid))

    def _probe_tick(self) -> None:
        if not self.running:
            return
        for i, ev in enumerate(self.evs):
            if ev.state == BAD:
                body = MData(self.peer_qpn, self.qpn, -1, 0, 0, 0, i, self.sim.now, probe=True)
                self.host.send(Packet(self.host.addr(ev.plane), self.peer.addr(ev.plane),
                                      49152 + i, PORT, 128, ECT0, body, via=ev.spine), ev.plane)
        self.sim.after(self.cfg.probe_ns, self._probe_tick)

    # -- acks ----------------------------------------------------------------------------

    def on_ack(self, a: MAck) -> None:
        now = self.sim.now
        if a.probe_ev is not None:
            ev = self.evs[a.probe_ev]
            if ev.state == BAD:
                ev.state, ev.bad_since = GOOD, None
                self.stats.flows[self._key(a.probe_ev)]["sports"].append([now / 1e9, ev.spine])
            return
        if a.echo_ts is not None:
            rtt = now - a.echo_ts
            if self.srtt is None:
                self.srtt, self.mdev = rtt, rtt / 2
            else:
                self.mdev = 0.75 * self.mdev + 0.25 * abs(rtt - self.srtt)
                self.srtt = 0.875 * self.srtt + 0.125 * rtt
        acked = [psn for psn in self.inflight if psn <= a.cum or psn in a.sacked]
        for psn in acked:
            rec = self.inflight.pop(psn)
            m = self.open_msgs.get(rec[2])
            if m is not None:
                m[0] += 1
                if m[0] >= self.npkts:
                    del self.open_msgs[rec[2]]
                    self.stats.msgs_done += 1
                    self.stats.fct_ns.append(now - m[1])
        for i in a.ce_evs:
            self.evs[i].avoid_until = now + self.cfg.avoid_ns
        ce = len(a.ce_evs)
        clean = max(0, len(acked) - ce)
        for _ in range(clean):
            self.qwnd = min(self.cfg.max_qwnd, self.qwnd + self.cfg.epsilon / max(self.qwnd, 1))
        if ce:
            self.qwnd = max(1.0, self.qwnd - self.cfg.beta * ce)
        if a.nack is not None and a.nack in self.inflight:
            self.qwnd = max(1.0, self.qwnd - self.cfg.beta)
            self._resend(a.nack, self.inflight[a.nack][0], timeout=False)
        self._arm()
        self.pump()


class MrcReceiver:
    def __init__(self, sim: Sim, host: Host, cfg: MrcConfig, qpn: int, stats: Stats):
        self.sim, self.host, self.cfg, self.qpn, self.stats = sim, host, cfg, qpn, stats
        self.cum = -1
        self.beyond: set = set()
        self.ce_evs: list = []
        self.since_ack = 0
        self.msgs: dict = {}

    def on_data(self, pkt: Packet, plane: int) -> None:
        d: MData = pkt.body
        if d.probe:
            self._reply(pkt, plane, MAck(d.sqp, self.cum, frozenset(), None, [], None,
                                         probe_ev=d.ev))
            return
        if pkt.trimmed:
            self._reply(pkt, plane, MAck(d.sqp, self.cum, frozenset(self.beyond), d.psn,
                                         self._take_ce(), d.tx_ts))
            return
        if pkt.ecn == CE:
            self.ce_evs.append(d.ev)
            self.stats._add(self.stats.ce, f"plane {plane}", self.sim.now, 1)
        fresh = d.psn > self.cum and d.psn not in self.beyond
        if fresh:
            self.beyond.add(d.psn)
            while self.cum + 1 in self.beyond:
                self.cum += 1
                self.beyond.discard(self.cum)
            got = self.msgs.setdefault(d.msn, set())
            if d.posn not in got:
                got.add(d.posn)
                self.stats._add(self.stats.goodput, f"plane {plane}", self.sim.now, pkt.size * 8)
                self.stats._add(self.stats.qp_bits, d.sqp, self.sim.now, pkt.size * 8)
                if len(got) >= d.npkts:
                    del self.msgs[d.msn]
        self.since_ack += 1
        if (not fresh or self.beyond or self.ce_evs or self.since_ack >= self.cfg.ack_every):
            self.since_ack = 0
            self._reply(pkt, plane, MAck(d.sqp, self.cum, frozenset(self.beyond), None,
                                         self._take_ce(), d.tx_ts))

    def _take_ce(self) -> list:
        ce, self.ce_evs = self.ce_evs, []
        if ce:
            self.stats._add(self.stats.cnp, "ecn echo", self.sim.now, len(ce))
        return ce

    def _reply(self, pkt: Packet, plane: int, ack: MAck) -> None:
        self.host.send(Packet(pkt.dst, pkt.src, pkt.sport, PORT, self.cfg.ack_wire_bytes, ECT0,
                              ack, via=pkt.via), plane)


class MrcEndpoint:
    def __init__(self, sim: Sim, host: Host):
        self.senders: dict = {}
        self.receivers: dict = {}
        host.handlers[PORT] = self.dispatch
        prev = [p.on_drain for p in host.nics.values()]
        for port, old in zip(host.nics.values(), prev):
            port.on_drain = (lambda old=old: (old() if old else None, self._drain()))

    def _drain(self) -> None:
        for s in self.senders.values():
            s.pump()

    def dispatch(self, pkt: Packet, plane: int) -> None:
        b = pkt.body
        if isinstance(b, MAck):
            s = self.senders.get(b.dqp)
            if s is not None:
                s.on_ack(b)
        else:
            r = self.receivers.get(b.dqp)
            if r is not None:
                r.on_data(pkt, plane)
