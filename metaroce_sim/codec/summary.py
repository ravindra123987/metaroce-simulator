"""Per-flow summary of a MetaRoCE capture (`mrsim pcap summarize`)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .headers import AckKind
from .pcap import Frame


@dataclass
class FlowStats:
    """Data direction of one MetaRoCE flow: (src addr, dst addr, dst QP, flow ID)."""

    data_pkts: int = 0
    payload_bytes: int = 0  # UDP payload bytes of data packets
    sports: set = field(default_factory=set)
    retx: int = 0
    retx_timeout: int = 0
    ack_req: int = 0
    ce_marked: int = 0
    max_fsn: int | None = None
    ops: dict = field(default_factory=lambda: defaultdict(int))
    tags: set = field(default_factory=set)
    # Filled from ACKs travelling the other way.
    acks: int = 0
    sacks: int = 0
    naks: int = 0
    rnr: int = 0
    cnp_total: int = 0
    rate_hints: list = field(default_factory=list)
    first_ns: int | None = None
    last_ns: int | None = None


@dataclass
class Summary:
    frames: int = 0
    decode_errors: int = 0
    mcrc_bad: int = 0
    controls: int = 0
    flows: dict = field(default_factory=dict)


def summarize(frames) -> Summary:
    s = Summary()
    for fr in frames:
        _add(s, fr)
    return s


def _flow(s: Summary, key) -> FlowStats:
    st = s.flows.get(key)
    if st is None:
        st = s.flows[key] = FlowStats()
    return st


def _add(s: Summary, fr: Frame) -> None:
    s.frames += 1
    if fr.packet is None:
        s.decode_errors += 1
        return
    p = fr.packet
    if p.mcrc_valid is False:
        s.mcrc_bad += 1
    if p.ack is not None:
        # An ACK from B to A acknowledges the A->B flow whose destination QP
        # is this ACK's source QP.
        st = _flow(s, (fr.dst, fr.src, p.base.src_qp, p.base.flow_id))
        st.acks += 1
        kind = p.ack.kind
        if kind == AckKind.SACK:
            st.sacks += 1
        elif kind in (AckKind.BRNR, AckKind.HRNR):
            st.rnr += 1
        elif kind == AckKind.NAK:
            st.naks += 1
        st.cnp_total += p.ack.cnp
        if p.ack.rate_hint:
            st.rate_hints.append(p.ack.rate_hint)
        return
    if p.data is None:
        s.controls += 1
        return
    st = _flow(s, (fr.src, fr.dst, p.base.dst_qp, p.base.flow_id))
    st.data_pkts += 1
    st.payload_bytes += fr.udp_len or len(p.payload)
    st.sports.add(fr.sport)
    st.ops[p.op.name if p.op else "?"] += 1
    st.tags.add(p.data.tag)
    st.retx += p.data.retx
    st.retx_timeout += p.data.timeout
    st.ack_req += p.data.ack_req
    st.ce_marked += fr.ecn == 3
    st.max_fsn = p.data.fsn if st.max_fsn is None else max(st.max_fsn, p.data.fsn)
    st.first_ns = fr.ts_ns if st.first_ns is None else st.first_ns
    st.last_ns = fr.ts_ns


def to_dict(s: Summary) -> dict:
    flows = []
    for (src, dst, qp, fid), st in sorted(s.flows.items(), key=lambda kv: kv[0]):
        dur = ((st.last_ns or 0) - (st.first_ns or 0)) / 1e9
        flows.append({
            "src": src, "dst": dst, "dst_qp": qp, "flow_id": fid,
            "data_pkts": st.data_pkts, "payload_bytes": st.payload_bytes,
            "gbps": round(st.payload_bytes * 8 / dur / 1e9, 3) if dur > 0 else None,
            "sports": sorted(st.sports), "retx": st.retx, "retx_timeout": st.retx_timeout,
            "ack_req": st.ack_req, "ce_marked": st.ce_marked, "max_fsn": st.max_fsn,
            "ops": dict(st.ops), "tags": sorted(st.tags),
            "acks": st.acks, "sacks": st.sacks, "naks": st.naks, "rnr": st.rnr,
            "cnp_total": st.cnp_total,
            "rate_hint_last": st.rate_hints[-1] if st.rate_hints else None,
        })
    return {
        "frames": s.frames, "decode_errors": s.decode_errors, "mcrc_bad": s.mcrc_bad,
        "controls": s.controls, "flows": flows,
    }


def render(s: Summary) -> str:
    d = to_dict(s)
    lines = [
        (
            f"frames={d['frames']} decode_errors={d['decode_errors']} "
            f"mcrc_bad={d['mcrc_bad']} control={d['controls']} flows={len(d['flows'])}"
        ),
        "",
        (
            f"{'src -> dst':44} {'qp':>8} {'flow':>4} {'pkts':>7} {'MB':>8} {'sports':>6} "
            f"{'retx':>5} {'rto':>4} {'CE':>5} {'acks':>6} {'sack':>5} {'cnp':>5} {'rate':>5}"
        ),
    ]
    for f in d["flows"]:
        ends = f"{f['src']} -> {f['dst']}"
        lines.append(
            f"{ends[:44]:44} {f['dst_qp']:>8x} {f['flow_id']:>4} {f['data_pkts']:>7} "
            f"{f['payload_bytes'] / 1e6:>8.2f} {len(f['sports']):>6} {f['retx']:>5} "
            f"{f['retx_timeout']:>4} {f['ce_marked']:>5} {f['acks']:>6} {f['sacks']:>5} "
            f"{f['cnp_total']:>5} {f['rate_hint_last'] if f['rate_hint_last'] is not None else '-':>5}"
        )
    return "\n".join(lines)


__all__ = ["FlowStats", "Summary", "render", "summarize", "to_dict"]
