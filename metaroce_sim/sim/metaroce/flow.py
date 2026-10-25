"""Per-flow reliability state (spec §4): FSNs, SACK, the retransmit cursor,
RTT/RTO estimation. Pure logic, no network, so the spec's examples can be
replayed directly (tests/test_sim_flow.py replays Figure 4.8).

FSNs are kept as unbounded integers here; the 16-bit wrap only matters on
the wire.
"""

from __future__ import annotations

from dataclasses import dataclass, field

_ABSENT = object()


@dataclass
class Ack:
    """What the sender needs from an SAETH (§8.5)."""

    cum_fsn: int                     # highest FSN such that all <= it arrived
    max_fsn: int
    sacked: frozenset = frozenset()  # FSNs > cum_fsn that arrived
    cnp: int = 0
    rate_gbps: int = 0
    echo_ts: int | None = None       # tx timestamp of the packet that asked for this ack


@dataclass
class TxFlow:
    flow_id: int
    entropy: int
    plane: int
    snd_una: int = 0     # oldest FSN not known delivered
    snd_nxt: int = 0     # next new FSN
    snd_cur: int = 0     # retransmit cursor (§4.6)
    fwnd: int = 0
    inflight: dict = field(default_factory=dict)   # fsn -> frame (unacked)
    delivered: set = field(default_factory=set)    # sacked FSNs >= snd_una
    srtt_ns: float | None = None
    mdev_ns: float = 0.0
    rtos_without_ack: int = 0
    active: bool = False
    retx_queue: list = field(default_factory=list)  # FSNs to resend, in order
    last_frames: list = field(default_factory=list)  # frames released by the last ACK

    # -- sending -----------------------------------------------------------------

    def outstanding(self) -> int:
        return len(self.inflight)

    def room(self) -> int:
        return self.fwnd - self.outstanding()

    def take_fsn(self, frame) -> int:
        fsn = self.snd_nxt
        self.snd_nxt += 1
        self.inflight[fsn] = frame
        return fsn

    # -- acknowledgment ----------------------------------------------------------

    def on_ack(self, ack: Ack, sack_mode: str = "immediate") -> tuple[list, list]:
        """Apply an ACK. Returns (newly delivered FSNs, FSNs to retransmit now)."""
        newly = []
        self.last_frames = []
        inflight = self.inflight
        # Only FSNs from snd_una up to the cumulative point, plus the selectively
        # acked ones, can be newly delivered: O(newly), not O(window).
        for fsn in range(self.snd_una, ack.cum_fsn + 1):
            fr = inflight.pop(fsn, _ABSENT)
            if fr is not _ABSENT:
                newly.append(fsn)
                self.last_frames.append(fr)
        for fsn in ack.sacked:
            fr = inflight.pop(fsn, _ABSENT)
            if fr is not _ABSENT:
                newly.append(fsn)
                self.last_frames.append(fr)
        if newly:
            self.rtos_without_ack = 0
        self.delivered.update(f for f in ack.sacked if f >= self.snd_una)
        old_una = self.snd_una
        self.snd_una = max(self.snd_una, ack.cum_fsn + 1)
        while self.snd_una in self.delivered:
            self.delivered.discard(self.snd_una)
            self.snd_una += 1
        if self.snd_una > old_una:
            self.delivered = {f for f in self.delivered if f >= self.snd_una}
        self.snd_cur = max(self.snd_cur, self.snd_una)

        resend = []
        if sack_mode == "immediate":
            # Each newly acknowledged packet allows one retransmission from the
            # cursor (Figs. 4.8, 4.10). The scan skips delivered FSNs, the cursor
            # moves only past what is resent (so duplicate SACKs resend nothing),
            # and nothing at or beyond MaxFSN is presumed lost (§4.6).
            allowance = len(newly)
            f = self.snd_cur
            while allowance and f < ack.max_fsn:
                if f in self.inflight and f not in self.delivered:
                    resend.append(f)
                    allowance -= 1
                    self.snd_cur = f + 1
                f += 1
        return newly, resend

    # -- RTT / RTO (§4.5) --------------------------------------------------------

    def rtt_sample(self, rtt_ns: float, beta: float) -> None:
        if self.srtt_ns is None:
            self.srtt_ns, self.mdev_ns = rtt_ns, rtt_ns / 2
        else:
            self.mdev_ns = (1 - beta) * self.mdev_ns + beta * abs(rtt_ns - self.srtt_ns)
            self.srtt_ns = (1 - 0.125) * self.srtt_ns + 0.125 * rtt_ns

    def rto_ns(self, lower: int, upper: int, last_rtt: float | None = None) -> int:
        base = upper if self.srtt_ns is None else self.srtt_ns + 4 * self.mdev_ns
        base = max(base, last_rtt or 0)
        rto = base * (2 ** min(self.rtos_without_ack, 16))
        return int(min(upper, max(lower, rto)))

    def on_rto(self) -> list:
        """Timeout: reset the cursor and return FSNs to resend from snd_una."""
        self.rtos_without_ack += 1
        self.snd_cur = self.snd_una
        resend = sorted(f for f in self.inflight if f not in self.delivered)
        if resend:
            self.snd_cur = resend[-1] + 1
        return resend
