"""Side-by-side report for runs of one scenario with different transports."""

from __future__ import annotations

import json
from html import escape
from pathlib import Path

from .report import _CHART_LIB, _CSS

LABEL = {"metaroce": "MetaRoCE", "mrc": "MRC", "emu": "emulator (LSMR)", "sim": "simulator (lsmr profile)"}


def _window_mean(series: list[float], t0: float, t1: float, bin_s: float) -> float | None:
    v = series[int(t0 / bin_s):int(t1 / bin_s)]
    return round(sum(v) / len(v), 3) if v else None


def metrics(d: dict) -> dict:
    bin_s, dur = d["bin_s"], d["duration"]
    total = d["goodput_gbps"]["total"]
    hits = [f for f in d["faults"] if f["action"] in ("netem", "link-down")]
    out = {
        "goodput_gbps": _window_mean(total, 1.0, dur, bin_s),
        "during_fault_gbps": None,
        "recover_s": [r["recover_s"] for r in d["recovery"]],
        "retx": sum(f["retx"] for f in d["flows"].values()),
        "rto": sum(f["rto"] for f in d["flows"].values()),
        # series are per second of the run's own clock (simulated time for sim runs)
        "ce": round(sum(sum(v) for v in d["ce_per_s"].values()) * bin_s
                    * (d.get("sim") or {}).get("time_scale", 1.0)),
        "msgs_done": (d.get("sim") or {}).get("msgs_done"),
        "mean_fct_us": (d.get("sim") or {}).get("mean_fct_us"),
    }
    if hits:
        f = hits[0]
        end = next((g["t"] for g in d["faults"] if g["t"] > f["t"]
                    and g["action"] in ("clear", "link-up")), dur)
        out["during_fault_gbps"] = _window_mean(total, f["t"] + 0.5, end, bin_s)
    return out


ROWS = [
    ("goodput_gbps", "mean goodput (Gbit/s)"),
    ("during_fault_gbps", "goodput during the fault (Gbit/s)"),
    ("recover_s", "back to 90% after fault (s)"),
    ("retx", "retransmissions"),
    ("rto", "of which by timeout"),
    ("ce", "CE-marked packets"),
    ("msgs_done", "messages completed"),
    ("mean_fct_us", "mean message completion time (µs, sim time)"),
]


def render(name: str, runs: dict[str, dict]) -> str:
    m = {tp: metrics(d) for tp, d in runs.items()}
    tps = list(runs)
    head = "".join(f"<th>{LABEL.get(tp, tp)}</th>" for tp in tps)
    body = "".join(
        f"<tr><td>{label}</td>" + "".join(f"<td>{escape(str(m[tp][key]))}</td>" for tp in tps)
        + "</tr>" for key, label in ROWS)
    first = next(iter(runs.values()))
    data = {"bin_s": first["bin_s"], "duration": first["duration"], "faults": first["faults"],
            "runs": {tp: {"total": d["goodput_gbps"]["total"],
                          **{k: v for k, v in d["goodput_gbps"].items() if k.startswith("plane")}}
                     for tp, d in runs.items()}}
    payload = json.dumps(data).replace("</", "<\\/")
    plane_divs = "".join(f'<h2>{LABEL.get(tp, tp)}: goodput per plane</h2>'
                         f'<div class="chart" id="c-{tp}"></div>' for tp in tps)
    main = r"""
const RUNS=D.runs, TP=Object.keys(RUNS), LBL={metaroce:'MetaRoCE',mrc:'MRC',emu:'emulator (LSMR)',sim:'simulator'};
chart('c-total', TP.map((tp,i)=>({n:LBL[tp]||tp, v:RUNS[tp].total, c:['var(--s1)','var(--s2)'][i%2]})), 'Gbit/s');
for(const tp of TP){ const ks=Object.keys(RUNS[tp]).filter(k=>k.startsWith('plane')).sort();
  chart('c-'+tp, ks.map((k,i)=>({n:k, v:RUNS[tp][k], c:['var(--s3)','var(--s4)'][i%2]})), 'Gbit/s'); }
"""
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(name)}: {' vs '.join(LABEL.get(t, t) for t in tps)}</title><style>{_CSS}</style></head>
<body class="viz-root"><div class="wrap">
<h1>{escape(name)}: {' vs '.join(LABEL.get(t, t) for t in tps)}</h1>
<p class="sub">Same scenario, same simulated fabric and congestion control baseline; only the
transport's path control and loss signalling differ.</p>
<div class="tbl"><table><tr><th>metric</th>{head}</tr>{body}</table></div>
<h2>Total goodput</h2><div class="chart" id="c-total"></div>
{plane_divs}
</div>
<script id="data" type="application/json">{payload}</script>
<script>{_CHART_LIB}{main}</script></body></html>"""


def write(out: Path, name: str, run_dirs: dict[str, Path]) -> Path:
    runs = {tp: json.loads((p / "report.json").read_text()) for tp, p in run_dirs.items()}
    out.mkdir(parents=True, exist_ok=True)
    (out / "compare.json").write_text(json.dumps({tp: metrics(d) for tp, d in runs.items()},
                                                 indent=1))
    html = out / "compare.html"
    html.write_text(render(name, runs))
    return html
