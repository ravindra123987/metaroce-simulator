"""mrsim: MetaRoCE emulator and simulator."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from metaroce_sim import __version__


def _pcap_summarize(args) -> int:
    from metaroce_sim.codec.pcap import read_frames
    from metaroce_sim.codec.summary import render, summarize, to_dict

    with open(args.file, "rb") as f:
        s = summarize(read_frames(f, port=args.port, has_mcrc=not args.no_mcrc))
    print(json.dumps(to_dict(s), indent=2) if args.json else render(s))
    return 0


def _workdir(args, t) -> Path:
    return Path(args.workdir or Path.home() / "metaroce-labs" / t.name)


def _topo_gen(args) -> int:
    from metaroce_sim.topo import gen, model

    t = model.load(args.topology)
    print(gen.write(t, _workdir(args, t)))
    return 0


def _emu_up(args) -> int:
    from metaroce_sim.emu import lab
    from metaroce_sim.topo import model

    t = model.load(args.topology)
    print(lab.up(t, _workdir(args, t)))
    return 0


def _emu_down(args) -> int:
    from metaroce_sim.emu import lab
    from metaroce_sim.topo import model

    t = model.load(args.topology)
    lab.down(t, _workdir(args, t))
    return 0


def _emu_health(args) -> int:
    from metaroce_sim.emu import lab
    from metaroce_sim.topo import model

    t = model.load(args.topology)
    pairs = [(args.src, args.dst)] if args.dst is not None else [
        (0, n) for n in range(1, t.num_hosts)]
    bad = lab.health(t, pairs)
    print(json.dumps({"healthy": not bad, "bad": bad}))
    return 0 if not bad else 1


def _emu_ecmp_check(args) -> int:
    from metaroce_sim.emu import lab
    from metaroce_sim.topo import model

    t = model.load(args.topology)
    planes = range(t.planes) if args.plane is None else [args.plane]
    worst = 1.0
    for p in planes:
        r = lab.ecmp_check(t, args.src, args.dst, p, ports=args.ports)
        worst = min(worst, r["p_value"])
        print(json.dumps(r))
    return 0 if worst > args.alpha else 1


def _emu_run(args) -> int:
    from metaroce_sim import scenario
    from metaroce_sim.emu import runner
    from metaroce_sim.topo import model

    raw = scenario.yaml.safe_load(Path(args.scenario).read_text())
    t = model.load(args.topology or raw["topology"])
    sc = scenario.parse(raw, t.num_hosts)
    out = runner.run(t, sc, Path(args.results))
    print(out)
    return 0


def _report(args) -> int:
    from metaroce_sim.analysis import report

    for run in args.runs:
        print(*report.write(run))
    return 0


def _sim_run(args) -> int:
    from metaroce_sim import scenario
    from metaroce_sim.sim import run as simrun
    from metaroce_sim.topo import model

    raw = scenario.yaml.safe_load(Path(args.scenario).read_text())
    if args.time_scale is not None:
        raw.setdefault("sim", {})["time_scale"] = args.time_scale
    t = model.load(args.topology or raw["topology"])
    sc = scenario.parse(raw, t.num_hosts)
    for tp in args.transport.split(","):
        print(simrun.run(t, sc, Path(args.results), transport=tp.strip()))
    return 0


def _sim_compare(args) -> int:
    import time

    from metaroce_sim import scenario
    from metaroce_sim.analysis import compare
    from metaroce_sim.sim import run as simrun
    from metaroce_sim.topo import model

    raw = scenario.yaml.safe_load(Path(args.scenario).read_text())
    if args.time_scale is not None:
        raw.setdefault("sim", {})["time_scale"] = args.time_scale
    t = model.load(args.topology or raw["topology"])
    sc = scenario.parse(raw, t.num_hosts)
    out = Path(args.results) / time.strftime(f"{sc.name}-compare-%Y%m%d-%H%M%S")
    dirs = {tp: simrun.run(t, sc, out, run_id=tp, transport=tp)
            for tp in args.transport.split(",")}
    html = compare.write(out, sc.name, dirs)
    print(html)
    print(json.dumps(json.loads((out / "compare.json").read_text()), indent=1))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="mrsim", description=__doc__)
    parser.add_argument("--version", action="version", version=f"mrsim {__version__}")
    sub = parser.add_subparsers(dest="cmd")

    pcap = sub.add_parser("pcap", help="decode MetaRoCE captures")
    pcap_sub = pcap.add_subparsers(dest="pcap_cmd", required=True)
    summ = pcap_sub.add_parser("summarize", help="per-flow summary of a capture")
    summ.add_argument("file")
    summ.add_argument("--port", type=int, default=2766, help="MetaRoCE UDP port")
    summ.add_argument("--no-mcrc", action="store_true", help="packets carry no trailing mCRC")
    summ.add_argument("--json", action="store_true")
    summ.set_defaults(func=_pcap_summarize)

    sm = sub.add_parser("sim", help="discrete-event simulation")
    sm_sub = sm.add_subparsers(dest="sim_cmd", required=True)
    sr = sm_sub.add_parser("run", help="simulate a scenario and write its report")
    sr.add_argument("scenario")
    sr.add_argument("--topology")
    sr.add_argument("--time-scale", type=float, dest="time_scale",
                    help="simulated seconds per scenario second (default 0.02)")
    sr.add_argument("--transport", default="metaroce",
                    help="metaroce, mrc, or a comma list to run both")
    sr.add_argument("--results", default="results-sim")
    sr.set_defaults(func=_sim_run)

    sc_ = sm_sub.add_parser("compare", help="run transports side by side, write compare.html")
    sc_.add_argument("scenario")
    sc_.add_argument("--topology")
    sc_.add_argument("--time-scale", type=float, dest="time_scale")
    sc_.add_argument("--transport", default="metaroce,mrc")
    sc_.add_argument("--results", default="results-sim")
    sc_.set_defaults(func=_sim_compare)

    rep = sub.add_parser("report", help="build report.json/report.html for emu run directories")
    rep.add_argument("runs", nargs="+")
    rep.set_defaults(func=_report)

    topo = sub.add_parser("topo", help="topology generation")
    topo_sub = topo.add_subparsers(dest="topo_cmd", required=True)
    g = topo_sub.add_parser("gen", help="write containerlab files for a topology")
    g.add_argument("topology")
    g.add_argument("--workdir", help="output dir (default ~/metaroce-labs/<name>)")
    g.set_defaults(func=_topo_gen)

    emu = sub.add_parser("emu", help="containerlab emulation (run on the containerlab host)")
    emu_sub = emu.add_subparsers(dest="emu_cmd", required=True)
    for name, fn, help_ in (("up", _emu_up, "generate and deploy the lab"),
                            ("down", _emu_down, "destroy the lab")):
        e = emu_sub.add_parser(name, help=help_)
        e.add_argument("topology")
        e.add_argument("--workdir")
        e.set_defaults(func=fn)
    er = emu_sub.add_parser("run", help="run a scenario on the deployed lab")
    er.add_argument("scenario")
    er.add_argument("--topology", help="override the scenario's topology file")
    er.add_argument("--results", default="results")
    er.set_defaults(func=_emu_run)
    eh = emu_sub.add_parser("health", help="check UDP delivery both ways on every plane")
    eh.add_argument("topology")
    eh.add_argument("--src", type=int, default=0)
    eh.add_argument("--dst", type=int, help="default: h00 to every other host")
    eh.set_defaults(func=_emu_health)
    ec = emu_sub.add_parser("ecmp-check", help="check UDP source ports spread across spines")
    ec.add_argument("topology")
    ec.add_argument("--src", type=int, default=0, help="source host number")
    ec.add_argument("--dst", type=int, default=-1, help="destination host number (default last)")
    ec.add_argument("--plane", type=int)
    ec.add_argument("--ports", type=int, default=256)
    ec.add_argument("--alpha", type=float, default=0.01)
    ec.add_argument("--workdir")
    ec.set_defaults(func=_emu_ecmp_check)

    args = parser.parse_args(argv)
    if getattr(args, "dst", None) == -1:
        from metaroce_sim.topo import model

        args.dst = model.load(args.topology).num_hosts - 1
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
