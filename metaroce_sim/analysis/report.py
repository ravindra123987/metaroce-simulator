"""Render a run's timeline (analysis.timeline.analyze) as report.json + report.html.

The HTML is self-contained: data is embedded and charts are drawn as SVG
by a small inline script (no external libraries), with a hover crosshair,
shaded fault windows, direct labels and a per-second data table.
"""

from __future__ import annotations

import json
from html import escape
from pathlib import Path

from .timeline import analyze

_CSS = """
.viz-root{color-scheme:light;--surface-1:#fcfcfb;--surface-2:#f1f1ef;--text-primary:#0b0b0b;
--text-secondary:#52514e;--text-muted:#77766f;--grid:#e3e2de;--fault:rgba(227,73,72,.10);
--fault-edge:#e34948;--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--total:#52514e;
--bad:#b42335;--bad-soft:#f9dde1}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{
color-scheme:dark;--surface-1:#1a1a19;--surface-2:#242422;--text-primary:#f0efec;
--text-secondary:#c3c2b7;--text-muted:#8f8e86;--grid:#33332f;--fault:rgba(230,103,103,.14);
--fault-edge:#e66767;--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--total:#c3c2b7;
--bad:#ff7b8a;--bad-soft:#3a1a20}}
:root[data-theme="dark"] .viz-root{color-scheme:dark;--surface-1:#1a1a19;--surface-2:#242422;
--text-primary:#f0efec;--text-secondary:#c3c2b7;--text-muted:#8f8e86;--grid:#33332f;
--fault:rgba(230,103,103,.14);--fault-edge:#e66767;--s1:#3987e5;--s2:#d95926;--s3:#199e70;
--s4:#c98500;--total:#c3c2b7;--bad:#ff7b8a;--bad-soft:#3a1a20}
body{margin:0;background:var(--surface-1);color:var(--text-primary);
font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:26px;margin:0 0 4px}h2{font-size:17px;margin:32px 0 8px}
.sub{color:var(--text-secondary);margin:0 0 16px}
.tiles{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}
.tile{background:var(--surface-2);border-radius:8px;padding:10px 14px;min-width:150px}
.tile b{display:block;font-size:22px;font-variant-numeric:tabular-nums}
.tile span{color:var(--text-secondary);font-size:12px}
.banner{background:var(--bad-soft);color:var(--text-primary);border-left:3px solid var(--bad);
padding:8px 12px;border-radius:0 6px 6px 0;margin:12px 0}
.chart{position:relative;overflow-x:auto}.chart svg{display:block;min-width:640px}
.legend{display:flex;flex-wrap:wrap;gap:14px;color:var(--text-secondary);font-size:12px;margin:2px 0 4px}
.legend i{display:inline-block;width:14px;height:2px;vertical-align:middle;margin-right:6px}
.tip{position:absolute;pointer-events:none;background:var(--surface-1);border:1px solid var(--grid);
border-radius:6px;padding:6px 8px;font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,.12);
white-space:nowrap;font-variant-numeric:tabular-nums}
table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--grid)}
th{color:var(--text-secondary);font-weight:600}
.tbl{overflow-x:auto}details{margin-top:8px}summary{cursor:pointer;color:var(--text-secondary)}
code{font-size:12px}
"""

_CHART_LIB = r"""
const D = JSON.parse(document.getElementById('data').textContent);
const BIN = D.bin_s, W = 1040, H = 236, L = 52, R = 70, T = 28, B = 28;
const svgNS = 'http://www.w3.org/2000/svg';
function el(tag, attrs, parent){const e=document.createElementNS(svgNS,tag);
  for(const k in attrs) e.setAttribute(k, attrs[k]); if(parent) parent.appendChild(e); return e;}
function nice(max){if(max<=0) return 1; const p=Math.pow(10,Math.floor(Math.log10(max)));
  for(const m of [1,2,2.5,5,10]) if(m*p>=max) return m*p; return 10*p;}
function chart(id, series, unit){
  const host=document.getElementById(id); const n=Math.max(...series.map(s=>s.v.length));
  const tEnd=Math.min(n*BIN, D.duration+3);
  const ymax=nice(Math.max(1e-9,...series.flatMap(s=>s.v)));
  const x=t=>L+(W-L-R)*t/tEnd, y=v=>T+(H-T-B)*(1-v/ymax);
  const svg=el('svg',{viewBox:`0 0 ${W} ${H}`,width:'100%',role:'img'},host);
  for(const f of D.faults){ if(!['netem','link-down'].includes(f.action)) continue;
    const end=(D.faults.find(g=>g.t>f.t && ['clear','link-up'].includes(g.action))||{t:D.duration}).t;
    el('rect',{x:x(f.t),y:T,width:Math.max(1,x(end)-x(f.t)),height:H-T-B,fill:'var(--fault)'},svg);
    el('line',{x1:x(f.t),x2:x(f.t),y1:T,y2:H-B,stroke:'var(--fault-edge)','stroke-width':1},svg);
    const lab=el('text',{x:x(f.t)+4,y:T+12,'font-size':11,fill:'var(--text-secondary)'},svg);
    lab.textContent=`${f.action} ${f.spec||''}`.trim();}
  for(let i=0;i<=4;i++){const v=ymax*i/4;
    el('line',{x1:L,x2:W-R,y1:y(v),y2:y(v),stroke:'var(--grid)','stroke-width':1},svg);
    const tx=el('text',{x:L-6,y:y(v)+4,'text-anchor':'end','font-size':11,fill:'var(--text-muted)'},svg);
    tx.textContent=+v.toFixed(2);}
  const step=tEnd>30?5:tEnd>12?2:1;
  for(let t=0;t<=tEnd+1e-9;t+=step){const tx=el('text',{x:x(t),y:H-8,'text-anchor':'middle',
    'font-size':11,fill:'var(--text-muted)'},svg); tx.textContent=t+'s';}
  const u=el('text',{x:4,y:12,'font-size':11,fill:'var(--text-muted)'},svg); u.textContent=unit;
  const labels=[];
  for(const s of series){const pts=s.v.slice(0,Math.ceil(tEnd/BIN)).map((v,i)=>`${x((i+.5)*BIN).toFixed(1)},${y(v).toFixed(1)}`);
    el('polyline',{points:pts.join(' '),fill:'none',stroke:s.c,'stroke-width':2,
      'stroke-linejoin':'round','stroke-dasharray':s.dash||''},svg);
    const last=s.v.slice(0,Math.ceil(tEnd/BIN)); let li=last.length-1; while(li>0 && last[li]===0) li--;
    labels.push({y:y(last[li]||0),t:s.n});}
  labels.sort((a,b)=>a.y-b.y); for(let i=1;i<labels.length;i++) if(labels[i].y-labels[i-1].y<13) labels[i].y=labels[i-1].y+13;
  for(const lb of labels){const tx=el('text',{x:W-R+6,y:lb.y+4,'font-size':11,fill:'var(--text-secondary)'},svg); tx.textContent=lb.t;}
  const cross=el('line',{y1:T,y2:H-B,stroke:'var(--text-muted)','stroke-width':1,visibility:'hidden'},svg);
  const tip=document.createElement('div'); tip.className='tip'; tip.hidden=true; host.appendChild(tip);
  const hit=el('rect',{x:L,y:T,width:W-L-R,height:H-T-B,fill:'transparent'},svg);
  hit.addEventListener('mousemove',ev=>{const r=svg.getBoundingClientRect();
    const px=(ev.clientX-r.left)*W/r.width; const i=Math.max(0,Math.floor((px-L)/(W-L-R)*tEnd/BIN));
    const cx=x((i+.5)*BIN); cross.setAttribute('x1',cx); cross.setAttribute('x2',cx); cross.setAttribute('visibility','visible');
    tip.innerHTML=`<b>${(i*BIN).toFixed(1)}–${((i+1)*BIN).toFixed(1)} s</b><br>`+series.map(s=>
      `<span style="color:${getComputedStyle(host).getPropertyValue(s.c.slice(4,-1))}">■</span> ${s.n}: ${(s.v[i]||0).toFixed(3)} ${unit}`).join('<br>');
    tip.hidden=false; const left=Math.min(r.width-tip.offsetWidth-4,(cx*r.width/W)+12);
    tip.style.left=left+'px'; tip.style.top='8px';});
  hit.addEventListener('mouseleave',()=>{cross.setAttribute('visibility','hidden'); tip.hidden=true;});
  const lg=document.createElement('div'); lg.className='legend';
  lg.innerHTML=series.map(s=>`<span><i style="background:${s.c}"></i>${s.n}</span>`).join('');
  host.insertBefore(lg, svg);
  // per-second table (relief for low-contrast hues; also the accessible view)
  const secs=Math.ceil(tEnd); let rows='';
  for(let sIdx=0;sIdx<secs;sIdx++){const per=1/BIN;
    rows+=`<tr><td>${sIdx}–${sIdx+1}</td>`+series.map(s=>{const a=s.v.slice(sIdx*per,(sIdx+1)*per);
      return `<td>${(a.reduce((p,q)=>p+q,0)/Math.max(1,a.length)).toFixed(3)}</td>`;}).join('')+'</tr>';}
  const det=document.createElement('details');
  det.innerHTML=`<summary>Data table (1 s means)</summary><div class="tbl"><table><tr><th>second</th>${series.map(s=>`<th>${s.n} (${unit})</th>`).join('')}</tr>${rows}</table></div>`;
  host.appendChild(det);
}
"""

_MAIN = r"""const PC=['var(--s1)','var(--s2)','var(--s3)','var(--s4)'];
const planes=o=>Object.keys(o).filter(k=>k.startsWith('plane')).sort();
const gp=D.goodput_gbps;
chart('c-goodput',[...planes(gp).map((k,i)=>({n:k,v:gp[k],c:PC[i%4]})),
  {n:'total',v:gp.total,c:'var(--total)',dash:'4 3'}],'Gbit/s');
const rx=D.retx_per_s, RK={sack:'SACK',nack:'NACK (trim)',timeout:'timeout'};
const kinds=Object.keys(rx).filter(k=>rx[k].some(v=>v>0)); const RC=['var(--s3)','var(--s4)'];
chart('c-retx',(kinds.length?kinds:['sack','timeout']).map((k,i)=>({n:RK[k]||k,v:rx[k]||[],c:RC[i%2]})),'pkts/s');
const ce=D.ce_per_s; if(planes(ce).length){chart('c-ce',planes(ce).map((k,i)=>({n:k,v:ce[k],c:PC[i%4]})),'pkts/s');}
else {document.getElementById('c-ce').textContent='No CE-marked packets in this run.';}
"""

_JS = _CHART_LIB + _MAIN


def _fmt(v, unit="") -> str:
    return "–" if v is None else f"{v}{unit}"


def render(data: dict) -> str:
    rec_rows = "".join(
        f"<tr><td>{r['t_fault']:.1f} s</td><td>{_fmt(r.get('t_clear'), ' s')}</td>"
        f"<td>{_fmt(r['baseline_gbps'], ' Gbit/s')}</td>"
        f"<td>{_fmt(r['min_gbps'], ' Gbit/s')}</td><td>{_fmt(r['recover_s'], ' s')}</td></tr>"
        for r in data["recovery"]) or "<tr><td colspan=5>No faults in this run.</td></tr>"
    flow_rows = "".join(
        f"<tr><td><code>{escape(k)}</code></td><td>{f['plane']}</td><td>{f['pkts']}</td>"
        f"<td>{f['retx']}</td><td>{f['rto']}</td><td>{len(f['sports'])}</td>"
        f"<td>{escape(', '.join(f'{t:.1f}s→{p}' for t, p in f['sports'][1:6]))}"
        f"{' …' if len(f['sports']) > 6 else ''}</td></tr>"
        for k, f in sorted(data["flows"].items()))
    gp = data["goodput_gbps"]["total"]
    busy = [v for v in gp if v > 0]
    mean = sum(busy) / len(busy) if busy else 0.0
    nretx = sum(f["retx"] for f in data["flows"].values())
    nrto = sum(f["rto"] for f in data["flows"].values())
    resteer = sum(max(0, len(f["sports"]) - 1) for f in data["flows"].values())
    worst = max((r["recover_s"] for r in data["recovery"] if r["recover_s"] is not None),
                default=None)
    crash = ""
    if data["crashed"]:
        crash = (f'<div class="banner"><b>{len(data["crashed"])} rdma_gen process(es) crashed</b> '
                 "(libsoftmetaroce watchdog SIGSEGV at teardown, see docs/findings/lsmr.md #1). "
                 "Everything below comes from packet captures and is unaffected.</div>")
    payload = json.dumps(data).replace("</", "<\\/")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(data['name'])} run report</title><style>{_CSS}</style></head>
<body class="viz-root"><div class="wrap">
<h1>{escape(data['name'])}</h1>
<p class="sub">{data['duration']:.0f} s run · {len(data['faults'])} fault events ·
{len(data['flows'])} sender flows · bins of {data['bin_s']*1000:.0f} ms</p>
{crash}
<div class="tiles">
<div class="tile"><b>{mean:.2f}</b><span>mean goodput, Gbit/s</span></div>
<div class="tile"><b>{nretx}</b><span>retransmissions ({nrto} by timeout)</span></div>
<div class="tile"><b>{resteer}</b><span>flow re-steers (new source port)</span></div>
<div class="tile"><b>{_fmt(worst, ' s')}</b><span>slowest recovery to 90%</span></div>
</div>
<h2>Goodput at receivers</h2><div class="chart" id="c-goodput"></div>
<h2>Retransmissions at senders</h2><div class="chart" id="c-retx"></div>
<h2>CE-marked packets at receivers</h2><div class="chart" id="c-ce"></div>
<h2>Recovery after faults</h2><div class="tbl"><table>
<tr><th>fault at</th><th>cleared at</th><th>pre-fault mean</th><th>lowest (0.5 s avg)</th><th>back to 90%</th></tr>
{rec_rows}</table></div>
<h2>Sender flows</h2><div class="tbl"><table>
<tr><th>flow / EV</th><th>plane</th><th>packets</th><th>retx</th><th>RTO</th><th>paths used</th>
<th>changes (time→port or spine; −1 = retired)</th></tr>{flow_rows}</table></div>
</div>
<script id="data" type="application/json">{payload}</script>
<script>{_JS}</script></body></html>"""


def write(run: str | Path) -> tuple[Path, Path]:
    run = Path(run)
    data = analyze(run)
    (run / "report.json").write_text(json.dumps(data, indent=1))
    html = run / "report.html"
    html.write_text(render(data))
    return run / "report.json", html
