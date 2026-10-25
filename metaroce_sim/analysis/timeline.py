"""Per-run timelines from packet captures.

Works only from what is on the wire (captures on every host NIC), so it
does not depend on rdma_gen surviving teardown (docs/findings/lsmr.md #1).

Times are seconds since the scenario's clients started.
"""

from __future__ import annotations

import ipaddress
import json
from collections import defaultdict
from pathlib import Path

from metaroce_sim.codec.pcap import read_frames
from metaroce_sim.topo import model

BIN = 0.1  # seconds


class Series:
    """Per-second rates in BIN-second bins, keyed by a label."""

    def __init__(self) -> None:
        self.data: dict = defaultdict(lambda: defaultdict(float))

    def add(self, label: str, t: float, v: float = 1.0) -> None:
        self.data[label][int(t // BIN)] += v / BIN

    def to_lists(self, nbins: int) -> dict[str, list[float]]:
        return {k: [round(v.get(i, 0.0), 4) for i in range(nbins)]
                for k, v in sorted(self.data.items())}


def load_events(run: Path) -> tuple[float, float, list[dict]]:
    """(wall clock of t=0, clients-start offset, events)."""
    evs = [json.loads(line) for line in (run / "events.jsonl").read_text().splitlines()]
    wall0 = next((e["wall0"] for e in evs if "wall0" in e), None)
    if wall0 is None:
        raise ValueError(f"{run}/events.jsonl has no wall0 record (re-run with this version)")
    start = next(e["t"] for e in evs if e["kind"] == "clients-start")
    return wall0, start, evs


def analyze(run: str | Path) -> dict:
    run = Path(run)
    sc = json.loads((run / "scenario.json").read_text())
    topo = model.load(sc["topology"])
    wall0, start, evs = load_events(run)
    t_start = wall0 + start
    duration = float(sc["duration"])
    nbins = int((duration + 5) / BIN)
    dsts = {b for tr in sc["traffic"] for _, b in tr["pairs"]}

    goodput = Series()  # Gbit/s at receivers, first transmissions only
    retx = Series()     # retransmissions/s at senders, sack vs timeout
    ce = Series()       # CE-marked data packets/s at receivers
    cnp = Series()      # CNP sum/s in ACKs leaving receivers
    flows: dict = {}

    for hdir in sorted(p for p in run.iterdir() if p.is_dir() and p.name.startswith("h")):
        host = int(hdir.name[1:])
        mine = {str(ipaddress.IPv6Address(topo.host_addr(host, p))) for p in range(topo.planes)}
        for pcap in sorted(hdir.glob("eth*.pcap")):
            plane = int(pcap.stem[3:]) - 1
            label = f"plane {plane}"
            with pcap.open("rb") as fh:
                for fr in read_frames(fh):
                    p = fr.packet
                    if p is None:
                        continue
                    t = fr.ts_ns / 1e9 - t_start
                    if not 0 <= t < nbins * BIN:
                        continue
                    inbound = fr.dst in mine
                    if p.data is not None and inbound and host in dsts:
                        if not p.data.retx:
                            goodput.add(label, t, fr.udp_len * 8 / 1e9)
                        if fr.ecn == 3:
                            ce.add(label, t)
                    elif p.data is not None and not inbound:
                        key = f"{hdir.name}/qp{p.base.dst_qp:x}/flow{p.base.flow_id}"
                        st = flows.setdefault(key, {"plane": plane, "pkts": 0, "retx": 0,
                                                    "rto": 0, "sports": []})
                        st["pkts"] += 1
                        if not st["sports"] or st["sports"][-1][1] != fr.sport:
                            st["sports"].append([round(t, 3), fr.sport])
                        if p.data.retx:
                            st["retx"] += 1
                            st["rto"] += p.data.timeout
                            retx.add("timeout" if p.data.timeout else "sack", t)
                    elif p.ack is not None and not inbound and host in dsts and p.ack.cnp:
                        cnp.add(label, t, p.ack.cnp)

    faults = [{"t": round(e["t"] - start, 3), "action": e["action"], "target": e["target"],
               "spec": e.get("spec", "")}
              for e in evs if e["kind"] == "fault" and e["t"] - start < duration]
    gp = goodput.to_lists(nbins)
    total = [round(sum(v[i] for v in gp.values()), 4) for i in range(nbins)]
    status = run / "status.json"
    return {
        "name": sc["name"], "bin_s": BIN, "duration": duration, "faults": faults,
        "goodput_gbps": {"total": total, **gp},
        "retx_per_s": retx.to_lists(nbins),
        "ce_per_s": ce.to_lists(nbins),
        "cnp_per_s": cnp.to_lists(nbins),
        "flows": flows,
        "recovery": [recovery(total, f["t"], _cleared(faults, f, duration), duration)
                     for f in faults if f["action"] in ("netem", "link-down")],
        "crashed": json.loads(status.read_text())["crashed"] if status.exists() else [],
    }


def _cleared(faults: list[dict], f: dict, duration: float) -> float:
    """When the fault `f` was undone (matching clear/link-up), else the run end."""
    undo = {"netem": ("clear",), "link-down": ("link-up", "clear")}[f["action"]]
    later = [g["t"] for g in faults if g["t"] > f["t"] and g["action"] in undo
             and g["target"] == f["target"]]
    return min(later, default=duration)


def recovery(total: list[float], t_fault: float, t_clear: float | None = None,
             duration: float | None = None, before_s: float = 2.0, after_s: float = 3.0,
             frac: float = 0.9, window: int = 5) -> dict:
    """Throughput dip after a fault and the time to return to `frac` of the
    pre-fault mean (moving average over `window` bins).

    Only bins from the fault to `after_s` past its clearing are considered,
    and never past the end of the run's traffic (`duration`), so the
    end-of-run drop is not mistaken for the fault's effect.
    """
    i0 = round(t_fault / BIN)
    pre = total[max(0, i0 - int(before_s / BIN)):i0]
    base = sum(pre) / len(pre) if pre else 0.0
    end_t = len(total) * BIN if t_clear is None else t_clear + after_s
    if duration is not None:
        end_t = min(end_t, duration)
    post = total[i0:max(i0 + 1, round(end_t / BIN))]
    out = {"t_fault": t_fault, "t_clear": t_clear, "baseline_gbps": round(base, 3),
           "min_gbps": None, "recover_s": None}
    if not post or base <= 0:
        return out
    smooth = [sum(post[i:i + window]) / len(post[i:i + window])
              for i in range(max(1, len(post) - window + 1))]
    out["min_gbps"] = round(min(smooth), 3)
    dipped = next((i for i, v in enumerate(smooth) if v < frac * base), None)
    if dipped is None:
        out["recover_s"] = 0.0
    else:
        back = next((i for i in range(dipped, len(smooth)) if smooth[i] >= frac * base), None)
        out["recover_s"] = None if back is None else round(back * BIN, 2)
    return out
