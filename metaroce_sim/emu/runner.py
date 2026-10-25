"""Run a scenario on a deployed lab: captures, traffic, timed faults, results.

Timeline:

    t=-2s  start tcpdump on every host NIC that carries scenario traffic
    t=-1s  start rdma_gen servers (one per destination host)
    t= 0   start rdma_gen clients; fault clock starts
    t=f.at apply each fault
    t=dur  wait for clients (bounded), stop servers and captures,
           clear faults, copy JSON and pcaps into results/<run-id>/

Results:

    results/<run-id>/scenario.yaml    the scenario as run
    results/<run-id>/events.jsonl     fault and lifecycle events with wall times
    results/<run-id>/<host>/          rdma_gen JSON, logs, per-NIC pcaps
    results/<run-id>/summary.json     `mrsim pcap summarize` of every pcap
"""

from __future__ import annotations

import json
import shlex
import threading
import time
from dataclasses import asdict
from pathlib import Path

from metaroce_sim.codec.pcap import read_frames
from metaroce_sim.codec.summary import summarize, to_dict
from metaroce_sim.emu import faults as faultlib
from metaroce_sim.emu import lab
from metaroce_sim.emu import qos as qoslib
from metaroce_sim.emu.lab import container, dexec, sh
from metaroce_sim.scenario import Fault, Scenario, Traffic
from metaroce_sim.topo.model import Topology

RDMA_GEN = "/opt/metaroce/bin/rdma_gen"
REMOTE_DIR = "/tmp/mrsim"
BASE_CM_PORT = 18515


def _gids(t: Topology, host: int) -> str:
    return ",".join(t.host_addr(host, p) for p in range(t.planes))


def server_cmd(t: Topology, sc: Scenario, dst: int, srcs: int, tr: Traffic, port: int) -> str:
    args = [
        RDMA_GEN, "--verbs_mode=libsoftmetaroce",
        f"--gid_addresses={_gids(t, dst)}",
        f"--op_type={tr.op}", f"--message_size={tr.msg_size}",
        f"--sqp_per_qp={tr.tags}", f"--qp_cap={max(tr.qps * srcs, 1)}",
        f"--num_clients_to_complete={srcs}", f"--rc_cm_port={port}",
        f"--duration_secs={int(sc.duration) + 10}",
        f"--json_path={REMOTE_DIR}/server-{port}.json",
    ]
    return " ".join(shlex.quote(a) for a in args + _transport_flags(sc, tr))


def client_cmd(t: Topology, sc: Scenario, src: int, dst: int, tr: Traffic, port: int) -> str:
    args = [
        RDMA_GEN, "--verbs_mode=libsoftmetaroce",
        f"--gid_addresses={_gids(t, src)}",
        f"--rc_servers={t.host_addr(dst, 0)}#{tr.qps}", f"--rc_connect_ports={port}#{tr.qps}",
        f"--op_type={tr.op}", f"--message_size={tr.msg_size}",
        f"--sqp_per_qp={tr.tags}", f"--duration_secs={int(sc.duration)}",
        # rdma_gen sends exactly --num_messages (default 0); the duration caps it.
        "--num_messages=1000000000",
        # Let the server drain before terminate; cuts the LSMR end-of-run
        # watchdog crash from ~4/5 to ~1/4 runs (docs/findings/lsmr.md #1).
        "--use_completion_barrier=true",
        f"--json_path={REMOTE_DIR}/client-{t.host(dst)}-{port}.json",
    ]
    return " ".join(shlex.quote(a) for a in args + _transport_flags(sc, tr))


# scenario `transport:` key -> libsoftmetaroce flag
LSMR_KEYS = {
    "cc": "cc_algorithm",
    "fast_start_burst": "fast_start_burst",
    "min_rto_us": "min_rto_us",
    "max_rto_us": "max_rto_us",
    "rtos_before_repath": "rtos_before_repath",
    "nic_rate_gbps": "nic_rate_gbps",
    "max_fwnd": "max_fwnd",
    "sack_retransmission_mode": "sack_retransmission_mode",
}


def _transport_flags(sc: Scenario, tr: Traffic) -> list[str]:
    """LSMR flags from the scenario's `transport:` block.

    `paths` sets the flow ceiling (--libsoftmetaroce_txflows/rxflows). LSMR
    opens flows adaptively as the window grows (spec §7.4), so a slow
    software path may use fewer. `cc: static:<fwnd>` forces them all open
    but crashes LSMR (docs/findings/lsmr.md #1).
    `flags:` passes anything else straight through.
    """
    # glog writes INFO/WARNING files (port pairs, failovers) under --log_dir.
    # LSMR defaults --libsoftmetaroce_slow_receiver=true, which caps the RTO at
    # 2 s (outside the spec's 1-100 ms upper-bound range, overriding
    # max_rto_us) and caused ~2 s whole-QP stalls under plane loss
    # (docs/findings/lsmr.md #5). Off by default here; a scenario's
    # transport.flags can turn it back on.
    out = [f"--log_dir={REMOTE_DIR}", "--logbufsecs=0",
           "--libsoftmetaroce_slow_receiver=false", f"--libsoftmetaroce_txflows={tr.paths}", f"--libsoftmetaroce_rxflows={max(tr.paths, 64)}"]
    for key, flag in LSMR_KEYS.items():
        if key in sc.transport:
            out.append(f"--libsoftmetaroce_{flag}={sc.transport[key]}")
    return out + [str(f) for f in sc.transport.get("flags", [])]


def _bg(t: Topology, node: str, cmd: str, log: str) -> None:
    sh(["sudo", "docker", "exec", "-d", container(t, node), "sh", "-c",
        f"mkdir -p {REMOTE_DIR}; exec {cmd} > {REMOTE_DIR}/{log} 2>&1"])


class Events:
    def __init__(self, path: Path):
        self._f = path.open("w")
        self._lock = threading.Lock()
        self.t0 = time.time()
        self.log("start", wall0=self.t0)

    def log(self, kind: str, at: float | None = None, **kw) -> None:
        with self._lock:
            rec = {"t": round((at or time.time()) - self.t0, 4), "kind": kind, **kw}
            self._f.write(json.dumps(rec) + "\n")
            self._f.flush()

    def close(self) -> None:
        self._f.close()


def _apply(t: Topology, fault: Fault, ev: Events, qos=None) -> None:
    started = time.time()
    cmds = faultlib.commands(t, fault, qos)
    for node, cmd in cmds:
        dexec(t, node, cmd, check=False)
    # Logged at the time the first port changed; applying every port takes a
    # while (one docker exec each), so the spread is recorded too.
    ev.log("fault", at=started, action=fault.action, target=fault.target, spec=fault.spec,
           ports=len(cmds), apply_s=round(time.time() - started, 3))


def run(t: Topology, sc: Scenario, results_root: Path, run_id: str | None = None) -> Path:
    run_id = run_id or time.strftime(f"{sc.name}-%Y%m%d-%H%M%S")
    out = results_root / run_id
    out.mkdir(parents=True, exist_ok=False)
    (out / "scenario.json").write_text(json.dumps(asdict(sc), indent=2))
    ev = Events(out / "events.jsonl")

    pairs = {pair for tr in sc.traffic for pair in tr.pairs}
    bad = lab.health(t, pairs)
    ev.log("health", bad=bad)
    if bad:
        ev.close()
        raise RuntimeError(f"fabric is not delivering before the run: {bad}")

    qos = qoslib.from_fabric(t, sc.fabric)
    if qos:
        for node, cmd in qos.setup():
            dexec(t, node, cmd)
        ev.log("qos", ports=len(qos.ports), rate=qos.rate, ecn=qos.ecn)

    hosts = sorted({h for tr in sc.traffic for pair in tr.pairs for h in pair})
    for h in hosts:
        dexec(t, t.host(h), f"rm -rf {REMOTE_DIR}; mkdir -p {REMOTE_DIR}; pkill tcpdump; "
                            "pkill rdma_gen; true", check=False)
        for p in range(t.planes):
            nic = t.host_nic(p)
            _bg(t, t.host(h), f"tcpdump -i {nic} -s {sc.capture['snaplen']} -U "
                              f"-w {REMOTE_DIR}/{nic}.pcap udp", f"tcpdump-{nic}.log")
    ev.log("capture-start", hosts=[t.host(h) for h in hosts])
    time.sleep(2)

    # One server per (traffic entry, destination); clients connect to its port.
    port = BASE_CM_PORT
    clients = []
    for tr in sc.traffic:
        by_dst: dict[int, list[int]] = {}
        for a, b in tr.pairs:
            by_dst.setdefault(b, []).append(a)
        for dst, srcs in sorted(by_dst.items()):
            _bg(t, t.host(dst), server_cmd(t, sc, dst, len(srcs), tr, port), f"server-{port}.log")
            clients += [(src, dst, tr, port) for src in srcs]
            port += 1
    ev.log("servers-start", count=port - BASE_CM_PORT)
    time.sleep(1)

    for src, dst, tr, cport in clients:
        _bg(t, t.host(src), client_cmd(t, sc, src, dst, tr, cport),
            f"client-{t.host(dst)}-{cport}.log")
    ev.log("clients-start", count=len(clients))

    timers = [threading.Timer(f.at, _apply, (t, f, ev, qos)) for f in sc.faults]
    for tm in timers:
        tm.start()
    time.sleep(sc.duration)
    for tm in timers:
        tm.cancel()

    deadline = time.time() + 15
    while time.time() < deadline:
        busy = [h for h in hosts if dexec(t, t.host(h), "pgrep -f 'rdma_gen.*--rc_servers'",
                                          check=False).strip()]
        if not busy:
            break
        time.sleep(1)
    ev.log("clients-done")

    for h in hosts:
        dexec(t, t.host(h), "pkill rdma_gen; sleep 1; pkill tcpdump; true", check=False)
    for f in sc.faults:
        if f.action in ("netem", "link-down"):
            _apply(t, Fault(sc.duration, "clear", f.target), ev, qos)
    if qos:
        for node, cmd in qos.teardown():
            dexec(t, node, cmd, check=False)
    ev.log("stopped")
    ev.close()

    summaries = {}
    for h in hosts:
        hdir = out / t.host(h)
        hdir.mkdir()
        sh(["sudo", "docker", "cp", f"{container(t, t.host(h))}:{REMOTE_DIR}/.", str(hdir)])
        sh(["sudo", "chown", "-R", f"{_uid()}:{_gid()}", str(hdir)])
        for pcap in sorted(hdir.glob("*.pcap")):
            with pcap.open("rb") as fh:
                summaries[f"{t.host(h)}/{pcap.name}"] = to_dict(summarize(read_frames(fh)))
    (out / "summary.json").write_text(json.dumps(summaries, indent=2))
    crashed = sorted(str(p.relative_to(out)) for p in out.glob("*/*.log")
                     if "*** Aborted at" in p.read_text(errors="replace"))
    (out / "status.json").write_text(json.dumps({"crashed": crashed}, indent=2))
    from metaroce_sim.analysis import report

    report.write(out)
    return out


def _uid() -> int:
    import os

    return os.getuid()


def _gid() -> int:
    import os

    return os.getgid()
