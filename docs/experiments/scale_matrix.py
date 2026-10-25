"""One cell of the 4-plane scale matrix: per-QP goodput, fairness, and how
evenly ECMP spread load over the spine links. See scale-4p.md.

    tr '\\n' '\\0' < docs/experiments/scale_jobs.txt | \\
        xargs -0 -P 6 -n 1 .venv/bin/python docs/experiments/scale_matrix.py

Each job is "topology|pairs JSON|label|transport|Config JSON"; it runs three
seeds (a permutation re-draws per seed) and prints one JSON line.
"""

import json
import os
import re
import statistics as st
import sys
from pathlib import Path

from metaroce_sim import scenario
from metaroce_sim.sim import run as simrun
from metaroce_sim.topo import model

OUT = Path(os.environ.get("OUT", "results/experiments"))
topo, pairs, label, transport, cfg = sys.argv[1].split("|")
pairs, cfg = json.loads(pairs), json.loads(cfg)
T = model.load(topo)


def frac(xs, pred):
    return round(sum(map(pred, xs)) / len(xs), 3)


rows = []
for seed in (1, 2, 3):
    p = dict(pairs)
    if "permutation" in p:
        p["permutation"] = seed
    raw = {"name": "scale", "topology": topo, "duration": "6s",
           "traffic": [{"pairs": p, "op": "write", "msg_size": "1MiB", "paths": 8}],
           "sim": {"time_scale": 0.01, "seed": seed, "config": cfg}}
    sc = scenario.parse(raw, T.num_hosts)
    d = simrun.run(T, sc, OUT, run_id=f"scale-{label}-{seed}", transport=transport)
    r = json.loads((d / "report.json").read_text())
    v = list(r["qp_gbps"].values())
    lu = r["link_util"]
    up = [u for n, u in lu.items()
          if (m := re.search(r"-leaf\d+:eth(\d+)$", n)) and int(m[1]) <= T.spines]
    dn = [u for n, u in lu.items() if "-spine" in n]
    rows.append({"agg": round(sum(v), 1), "min": round(min(v), 1),
                 "p10": round(sorted(v)[len(v) // 10], 1), "max": round(max(v), 1),
                 "jain": round(sum(v) ** 2 / (len(v) * sum(x * x for x in v)), 3),
                 "up_idle": frac(up, lambda u: u < 0.05), "up_sat": frac(up, lambda u: u > 0.95),
                 "dn_idle": frac(dn, lambda u: u < 0.05), "dn_sat": frac(dn, lambda u: u > 0.95)})
cap = T.num_hosts * T.planes * 10
print(json.dumps({"label": label, "cap": cap,
                  "agg_pct": round(100 * st.mean(r["agg"] for r in rows) / cap, 1),
                  "min": min(r["min"] for r in rows),
                  "jain": round(st.mean(r["jain"] for r in rows), 3), "runs": rows}))
