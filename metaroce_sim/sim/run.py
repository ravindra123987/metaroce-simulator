"""Run a scenario in the discrete-event simulator and write the same report
as the emulator (report.json + report.html).

Scenario `sim:` block (all optional):

    sim:
      rate: 10gbit        # every link
      prop_ns: 500        # per hop
      queue_kb: 1024      # per port, drop-tail limit when not RED
      time_scale: 0.02    # simulated seconds per scenario second
      max_msgs: 8         # outstanding RDMA writes per QP (rdma_gen's posted WQEs)
      completion: any     # inorder: a WQE slot frees only when all earlier messages are done
      shape_rate: 5gbit   # override fabric.shape.rate
      seed: 1

Times in the report are scenario seconds (simulated time / time_scale), so
charts line up with emulator runs of the same scenario.
"""

from __future__ import annotations

import itertools
import json
import random
import re
import time
from dataclasses import asdict
from pathlib import Path

from metaroce_sim.analysis import report
from metaroce_sim.analysis.timeline import BIN, _cleared, recovery
from metaroce_sim.emu import faults as faultlib
from metaroce_sim.emu import qos as qoslib
from metaroce_sim.scenario import Scenario
from metaroce_sim.topo.model import Topology

from .engine import Sim
from .fabric import Fabric, SimParams, rate_bps
from .metaroce.config import Config
from .metaroce.endpoint import Endpoint, ReceiverQp, SenderQp, Stats
from .mrc.endpoint import MrcConfig, MrcEndpoint, MrcReceiver, MrcSender
from .net import Red

_LOSS = re.compile(r"loss\s+([0-9.]+)%")


def _entropy_counter():
    it = itertools.cycle(range(49152, 65536))  # device-global counter (§7.1)
    return lambda: next(it)


PROFILES = {
    # libsoftmetaroce as measured in the emulator (docs/findings/lsmr.md):
    # CPU-bound ~2 Gbit/s per QP, LSMR's timer defaults, re-steer after two
    # RTOs, and no timeout-driven port failover (none was ever logged).
    "lsmr": {"min_rto_ns": 10_000_000, "max_rto_ns": 200_000_000,
             "rtos_before_repath": 2, "rtos_before_port_failover": 0, "max_gbps": 2.0},
}


def build_config(sc: Scenario) -> Config:
    cfg = Config()
    for k, v in PROFILES.get((sc.sim or {}).get("profile", ""), {}).items():
        setattr(cfg, k, v)
    for k, v in ((sc.sim or {}).get("config") or {}).items():  # raw Config overrides
        setattr(cfg, k, type(getattr(cfg, k))(v))
    tr = sc.transport
    for key, attr in (("fast_start_burst", "fast_start_burst"), ("max_fwnd", "max_fwnd"),
                      ("rtos_before_repath", "rtos_before_repath"),
                      ("sack_retransmission_mode", "sack_mode")):
        if key in tr:
            setattr(cfg, attr, type(getattr(cfg, attr))(tr[key]))
    if "min_rto_us" in tr:
        cfg.min_rto_ns = int(tr["min_rto_us"]) * 1000
    if "max_rto_us" in tr:
        cfg.max_rto_ns = int(tr["max_rto_us"]) * 1000
    if "nic_rate_gbps" in tr:
        cfg.nic_gbps = float(tr["nic_rate_gbps"])
    return cfg


def run(t: Topology, sc: Scenario, results_root: Path, run_id: str | None = None,
        transport: str | None = None) -> Path:
    """Simulate `sc` with transport 'metaroce' (default) or 'mrc'."""
    transport = transport or sc.transport.get("name", "metaroce")
    if transport not in ("metaroce", "mrc"):
        raise ValueError(f"unknown transport {transport!r}")
    simcfg = dict(getattr(sc, "sim", None) or {})
    scale = float(simcfg.get("time_scale", 0.02))
    rng = random.Random(simcfg.get("seed", 1))
    params = SimParams(rate_bps=rate_bps(simcfg.get("rate", "10gbit")),
                       prop_ns=int(simcfg.get("prop_ns", 500)),
                       queue_bytes=int(simcfg.get("queue_kb", 1024)) * 1024,
                       seed=int(simcfg.get("seed", 1)))
    cfg = build_config(sc)
    cfg.nic_gbps = params.rate_bps / 1e9 if "nic_rate_gbps" not in sc.transport else cfg.nic_gbps

    sim = Sim()
    fab = Fabric(sim, t, params)
    qos = qoslib.from_fabric(t, sc.fabric)
    if qos:
        shaped_rate = rate_bps(simcfg.get("shape_rate", qos.rate))
        red = None
        if qos.ecn:
            red = Red(int(qos.ecn["min_kb"] * 1024), int(qos.ecn["max_kb"] * 1024),
                      float(qos.ecn.get("prob", 0.1)), qos.limit_bytes)
        for ep in qos.ports:
            port = fab.ports[ep]
            port.rate_bps, port.red = shaped_rate, red
            port.limit = qos.limit_bytes

    bin_ns = int(BIN * scale * 1e9)
    stats = Stats(bin_ns)
    qpn = itertools.count(0x10)
    senders = []
    if transport == "mrc":
        mcfg = MrcConfig()
        if simcfg.get("trim", True):
            for port in fab.ports.values():
                if not port.name.startswith("h"):  # switch ports trim
                    port.trim_bytes = mcfg.trim_bytes
        meps = {h.number: MrcEndpoint(sim, h) for h in fab.hosts}
        for tr in sc.traffic:
            for a, b in tr.pairs:
                for _ in range(tr.qps):
                    sq, rq = next(qpn), next(qpn)
                    s = MrcSender(sim, fab.hosts[a], fab.hosts[b], mcfg, sq, rq, t.planes,
                                  t.spines, tr.msg_size, int(simcfg.get("max_msgs", 8)), stats,
                                  f"{t.host(a)}/qp{sq:x}")
                    meps[a].senders[sq] = s
                    meps[b].receivers[rq] = MrcReceiver(sim, fab.hosts[b], mcfg, rq, stats)
                    senders.append(s)
    eps = {h.number: Endpoint(sim, h, cfg) for h in fab.hosts} if transport == "metaroce" else {}
    for tr in sc.traffic if transport == "metaroce" else []:
        for a, b in tr.pairs:
            for _ in range(tr.qps):
                sq, rq = next(qpn), next(qpn)
                s = SenderQp(sim, fab.hosts[a], cfg, sq, fab.hosts[b], rq, t.planes,
                             tr.msg_size, int(simcfg.get("max_msgs", 8)),
                             eps[a].__dict__.setdefault("entropy", _entropy_counter()),
                             stats, rng, f"{t.host(a)}/qp{sq:x}")
                eps[a].senders[sq] = s
                eps[b].receivers[rq] = ReceiverQp(sim, fab.hosts[b], cfg, rq, stats, eps[b].rcn)
                senders.append(s)
    inorder = simcfg.get("completion", "any") == "inorder"
    for i, s in enumerate(senders):
        s.inorder = inorder
        sim.at(i * 100, s.start)  # stagger starts by 100 ns

    def apply(fault):
        for ep in faultlib.ports(t, fault.target):
            port = fab.ports[ep]
            if fault.action == "link-down":
                port.up = False
            elif fault.action == "netem":
                m = _LOSS.search(fault.spec)
                port.loss = float(m.group(1)) / 100 if m else 0.0
            else:  # link-up, clear
                port.up, port.loss = True, 0.0

    for f in sc.faults:
        sim.at(int(f.at * scale * 1e9), apply, f)
    end_ns = int(sc.duration * scale * 1e9)
    for s in senders:
        sim.at(end_ns, s.stop)

    wall = time.time()
    sim.run(until=end_ns + 20 * cfg.max_rto_ns)
    wall = time.time() - wall

    run_id = run_id or time.strftime(f"{sc.name}-sim-{transport}-%Y%m%d-%H%M%S")
    out = results_root / run_id
    out.mkdir(parents=True, exist_ok=False)
    (out / "scenario.json").write_text(json.dumps(asdict(sc), indent=2))
    data = to_report(sc, stats, scale, bin_ns, transport,
                     {s.qpn: s.name for s in senders})
    data["sim"] = {"time_scale": scale, "rate_gbps": params.rate_bps / 1e9,
                   "events_wall_s": round(wall, 2), "msgs_done": stats.msgs_done,
                   "mean_fct_us": round(sum(stats.fct_ns) / len(stats.fct_ns) / 1e3, 2)
                   if stats.fct_ns else None}
    # Utilization of every switch egress port over the traffic period: ECMP
    # collisions show up as some spine-facing links full while others idle.
    data["link_util"] = {n: round(pt.stats["tx_bytes"] * 8 / (pt.rate_bps * end_ns / 1e9), 4)
                         for n, pt in sorted(fab.ports.items()) if not n.startswith("h")}
    (out / "report.json").write_text(json.dumps(data, indent=1))
    (out / "report.html").write_text(report.render(data))
    return out


def to_report(sc: Scenario, stats: Stats, scale: float, bin_ns: int,
              transport: str = "metaroce", qp_names: dict | None = None) -> dict:
    nbins = int((sc.duration + 5) / BIN)
    bin_s_sim = bin_ns / 1e9

    def series(table: dict, per: float) -> dict:
        return {k: [round(v.get(i, 0.0) * per, 4) for i in range(nbins)]
                for k, v in sorted(table.items())}

    gp = series(stats.goodput, 1 / bin_s_sim / 1e9)  # bits per bin -> Gbit/s
    total = [round(sum(v[i] for v in gp.values()), 4) for i in range(nbins)]
    faults = [{"t": f.at, "action": f.action, "target": f.target, "spec": f.spec}
              for f in sc.faults]
    flows = {}
    for k, f in stats.flows.items():
        if not f["pkts"]:
            continue
        f = dict(f)
        f["sports"] = [[round(t / scale, 3), p] for t, p in f["sports"]]
        flows[k] = f
    # Per-QP goodput, averaged after the first second (fast start settles).
    lo, hi = int(1 / BIN), int(sc.duration / BIN)
    qps = {(qp_names or {}).get(q, f"qp{q:x}"):
           round(sum(v.get(i, 0.0) for i in range(lo, hi)) / ((hi - lo) * bin_s_sim) / 1e9, 4)
           for q, v in sorted(stats.qp_bits.items())}
    return {
        "name": f"{sc.name} (sim, {transport})", "transport": transport, "bin_s": BIN, "duration": sc.duration, "faults": faults,
        "goodput_gbps": {"total": total, **gp},
        "retx_per_s": series(stats.retx, 1 / bin_s_sim),
        "ce_per_s": series(stats.ce, 1 / bin_s_sim),
        "cnp_per_s": series(stats.cnp, 1 / bin_s_sim),
        "flows": flows,
        "qp_gbps": qps,
        "recovery": [recovery(total, f["t"], _cleared(faults, f, sc.duration), sc.duration)
                     for f in faults if f["action"] in ("netem", "link-down")],
        "crashed": [],
    }
