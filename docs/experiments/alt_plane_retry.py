"""Longest whole-QP stall under plane loss, with and without spec-style
retry on another plane (§4.8 / §7.5.4). See alt-plane-retry.md.

    .venv/bin/python docs/experiments/alt_plane_retry.py LABEL CONFIG_JSON SEEDS

CONFIG_JSON holds raw Config overrides; its "_sim" key holds extra `sim:`
knobs (e.g. completion). Results go to $OUT (default results/experiments).
SCEN picks the scenario (default plane-loss).

    alt_plane_retry.py none '{"_sim":{"completion":"inorder"}}' 1,2,3
    alt_plane_retry.py retry2 '{"_sim":{"completion":"inorder"},"rtos_before_port_failover":2}' 1,2,3
"""

import json
import os
import sys
from pathlib import Path

import yaml

from metaroce_sim import scenario
from metaroce_sim.sim import run as simrun
from metaroce_sim.topo import model

OUT = Path(os.environ.get("OUT", "results/experiments"))
SCEN = os.environ.get("SCEN", "plane-loss")
T = model.load("topologies/2p-2x4.yaml")


def longest_stall(total, t0=4.0, t1=15.0, bin_s=0.1):
    """Longest run of bins below a quarter of the pre-fault goodput, in seconds."""
    base = sum(total[20:50]) / 30
    best = cur = 0
    for v in total[int(t0 / bin_s):int(t1 / bin_s)]:
        cur = cur + 1 if v < 0.25 * base else 0
        best = max(best, cur)
    return round(best * bin_s, 1)


def run(label, cfg, seeds, scale=1.0, name=SCEN):
    cfg = dict(cfg)
    simx = cfg.pop("_sim", {})
    out = []
    for seed in seeds:
        raw = yaml.safe_load(Path(f"scenarios/{name}.yaml").read_text())
        raw["sim"] = {"profile": "lsmr", "time_scale": scale, "seed": seed, "config": cfg, **simx}
        d = simrun.run(T, scenario.parse(raw, T.num_hosts), OUT, run_id=f"{name}-{label}-{seed}")
        r = json.loads((d / "report.json").read_text())
        g = r["goodput_gbps"]["total"]
        out.append({"stall": longest_stall(g), "during": round(sum(g[55:100]) / 45, 2),
                    "rto": sum(f["rto"] for f in r["flows"].values()), "msgs": r["sim"]["msgs_done"]})
    print(label.ljust(34), out)
    return out


if __name__ == "__main__":
    run(sys.argv[1], json.loads(sys.argv[2]), [int(x) for x in sys.argv[3].split(",")])
