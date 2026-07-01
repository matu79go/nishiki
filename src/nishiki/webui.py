"""Live KOI web dashboard — a dependency-free local server (stdlib http.server + SSE).

Unifies the first-run KOI leaderboard and the live HUD into ONE browser dashboard that updates
in real time as the agent runs: cumulative cost, average KOI, and inline-SVG charts (KOI over
calls + cumulative cost). No model calls (the numbers come from koi_estimate, the local edge
estimator), no third-party deps, and it works offline (no CDN — the chart is hand-drawn SVG).

  nishiki live --web   →  serves http://127.0.0.1:PORT and opens the browser

Pure helpers (read_events / call_row / snapshot) are unit-tested; the serving loop is thin.
"""
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import koi_estimate, koi_report, live


def read_events(log):
    """All logged model calls (one per `live.jsonl` line that has a model), oldest first."""
    if not os.path.exists(log):
        return []
    out = []
    for ln in open(log, encoding="utf-8").read().splitlines():
        try:
            o = json.loads(ln)
        except ValueError:
            continue
        if isinstance(o, dict) and o.get("model") is not None:
            out.append(o)
    return out


def call_row(experiment, ev, basis, index):
    """Edge-estimate one logged call for EVERY candidate (no model call).

    `model`/`cost`/`koi` are the route actually used (for the cards); `per` maps every candidate
    model → its estimated {cost, koi} for THIS same input, so the charts can compare all models.
    """
    res = live.estimate_event(experiment, ev, basis=basis)
    route = koi_estimate.resolve_model(basis, ev.get("model"))
    per = {r["model"]: {"cost": r["cost_per_item"], "koi": r["koi"]} for r in res["rows"]}
    cur = per.get(route, {})
    return {
        "i": index,
        "model": route or "?",
        "in_tokens": ev.get("in_tokens"),
        "out_tokens": ev.get("out_tokens"),
        "cost": cur.get("cost"),
        "koi": cur.get("koi"),
        "latency_ms": ev.get("latency_ms"),     # actual-route wall time (separate axis; None if not probed)
        "per": per,
        "ts": ev.get("ts"),
    }


def _per_item_list(candidate):
    """Per-item detail list for a candidate (shape-detected). Delegates to the shared koi_estimate helper."""
    return koi_estimate.per_item_list(candidate)


def measured_points(basis):
    """Per-item 'points' from the latest scored run, in the SAME shape the live charts consume.

    For the Measured tab: point i = {i, model: reference, per: {model: {cost, koi}}, latency_ms}, where
    per[m].cost is item i's measured cost for candidate m and per[m].koi is the RUNNING KOI (running KPI ÷
    running avg cost) so the curve shows where KOI converges. Candidates align by item index. No model calls.
    """
    run_path = basis.get("run_path")
    if not run_path or not os.path.exists(run_path):
        return []
    run = json.load(open(run_path, encoding="utf-8"))
    measured = basis.get("measured") or {}
    cand_items, n = {}, 0
    for c in run.get("candidates", []):
        label = c.get("label") or c.get("key")
        items = _per_item_list(c) if label else None
        if items:
            cand_items[label] = items
            n = max(n, len(items))
    if not n:
        return []
    ref = basis.get("reference")
    run_cost = {m: 0.0 for m in cand_items}
    run_correct = {m: 0 for m in cand_items}
    points = []
    for i in range(n):
        per, lat = {}, None
        for m, items in cand_items.items():
            if i >= len(items):
                continue
            it = items[i]
            cost = it.get("cost")
            if cost is not None:
                run_cost[m] += cost
            cnt = i + 1
            g, p = it.get("gold"), it.get("pred")
            if g is not None and p is not None:                  # running accuracy when gold/pred recorded
                if g == p:
                    run_correct[m] += 1
                kpi_run = run_correct[m] / cnt
            else:                                                # else fall back to the run's measured KPI
                kpi_run = (measured.get(m) or {}).get("kpi")
            avg_cost = run_cost[m] / cnt if cnt else None
            koi = (kpi_run / avg_cost) if (kpi_run is not None and avg_cost) else None
            per[m] = {"cost": cost, "koi": koi}
            if m == ref:
                lat = it.get("latency_ms")
        points.append({"i": i, "model": ref, "per": per, "latency_ms": lat,
                       "cost": (per.get(ref) or {}).get("cost"),
                       "koi": (per.get(ref) or {}).get("koi")})
    return points


def snapshot(experiment, log, basis):
    """Full initial payload: the leaderboard (estimate, reused measured cost) + every logged call."""
    lead = koi_estimate.estimate(experiment, basis=basis)
    fk, _fn = koi_report._floors(os.path.join(experiment, "KOI.yaml"))
    meas = basis.get("measured") or {}
    rows = []
    for r in lead["rows"]:                                        # attach measured latency (separate axis)
        m = meas.get(r["model"]) or {}
        rows.append({**r, "latency_ms_p50": m.get("latency_ms_p50"),
                     "latency_ms_p95": m.get("latency_ms_p95")})
    calls = [call_row(experiment, ev, basis, i) for i, ev in enumerate(read_events(log))]
    return {
        "kpi_name": lead["kpi_name"],
        "reference": lead["reference"],
        "kpi_floor": fk,
        "rows": rows,
        "calls": calls,
        "measured": measured_points(basis),                      # per-item series for the Measured tab
    }


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, handler, *, experiment, log, basis, interval):
        super().__init__(addr, handler)
        self.experiment, self.log, self.basis, self.interval = experiment, log, basis, interval


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # noqa: D401 — silence the default access log
        pass

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, "text/html; charset=utf-8", _PAGE.encode("utf-8"))
        elif self.path.startswith("/events"):
            self._stream()
        elif self.path.startswith("/vendor/"):
            self._send_vendor(self.path)
        else:
            self._send(404, "text/plain", b"not found")

    def _send_vendor(self, path):
        """Serve a vendored static asset (uPlot js/css) from the package vendor/ dir."""
        rel = path.split("?", 1)[0].lstrip("/")                  # vendor/uplot/uPlot.iife.min.js
        base = os.path.dirname(os.path.abspath(__file__))
        full = os.path.normpath(os.path.join(base, rel))
        if not full.startswith(os.path.join(base, "vendor")) or not os.path.isfile(full):
            self._send(404, "text/plain", b"not found")
            return
        if full.endswith(".css"):
            ctype = "text/css; charset=utf-8"
        elif full.endswith(".js"):
            ctype = "application/javascript; charset=utf-8"
        elif full.endswith(".png"):
            ctype = "image/png"                          # binary: no charset
        elif full.endswith(".svg"):
            ctype = "image/svg+xml; charset=utf-8"
        else:
            ctype = "text/plain; charset=utf-8"
        with open(full, "rb") as f:
            self._send(200, ctype, f.read())

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")     # local dev dashboard: always serve fresh (no stale UI)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _sse(self, event, payload):
        msg = f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        self.wfile.write(msg.encode("utf-8"))
        self.wfile.flush()

    def _stream(self):
        s = self.server
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self._sse("snapshot", snapshot(s.experiment, s.log, s.basis))
            sent = len(read_events(s.log))
            while True:
                evs = read_events(s.log)
                if len(evs) > sent:
                    for i in range(sent, len(evs)):
                        self._sse("call", call_row(s.experiment, evs[i], s.basis, i))
                    sent = len(evs)
                else:
                    self._sse("ping", {"t": time.strftime("%H:%M:%S")})
                time.sleep(s.interval)
        except (BrokenPipeError, ConnectionResetError):
            return


def serve(experiment, log, *, port=8765, interval=0.5, open_browser=True):
    """Start the dashboard server (blocks until Ctrl-C). Returns the bound port."""
    basis = koi_estimate.load_basis(experiment)
    if not basis.get("measured"):
        print("No measured run yet — do a scored run first so the estimate has a basis (KPI/cost).")
        return None
    httpd = _Server(("127.0.0.1", port), _Handler,
                    experiment=experiment, log=log, basis=basis, interval=interval)
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}"
    print(f"[live --web] KOI dashboard → {url}   (no model calls; Ctrl-C to stop)")
    print(f"             first measurement → {url}/#measured   ·   Live (per call) → {url}/#live")
    print(f"             events ← {log}")
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        httpd.server_close()
    return port


_PAGE = r"""<!doctype html><html lang=en><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Nishiki — live KOI dashboard</title>
<link rel=stylesheet href="/vendor/uplot/uPlot.min.css">
<script src="/vendor/uplot/uPlot.iife.min.js"></script>
<style>
:root{--bg:#0f1419;--card:#1a212b;--ln:#2a3542;--fg:#e6edf3;--mut:#8b98a5;--ac:#4a90d9;--ok:#3fb950;--warn:#d29922}
*{box-sizing:border-box}body{font:14px/1.55 system-ui,-apple-system,sans-serif;margin:0;background:var(--bg);color:var(--fg)}
header{display:flex;align-items:center;gap:.6rem;padding:1rem 1.4rem;border-bottom:1px solid var(--ln)}
header h1{font-size:1.1rem;margin:0;font-weight:600}.dot{width:.6rem;height:.6rem;border-radius:50%;background:var(--mut)}
.dot.on{background:var(--ok);box-shadow:0 0 8px var(--ok)}.spacer{flex:1}#upd{color:var(--mut);font-size:.85rem}
main{padding:1.2rem 1.4rem;max-width:1100px;margin:0 auto}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:1rem;margin-bottom:1.2rem}
.card{background:var(--card);border:1px solid var(--ln);border-radius:12px;padding:1rem 1.2rem}
.card .k{color:var(--mut);font-size:.78rem;text-transform:uppercase;letter-spacing:.04em}
.card .v{font-size:2rem;font-weight:700;font-variant-numeric:tabular-nums;margin:.15rem 0}
.card .s{color:var(--mut);font-size:.82rem}.card .s b{color:var(--fg);font-weight:600}
.panel{background:var(--card);border:1px solid var(--ln);border-radius:12px;padding:1rem 1.2rem;margin-bottom:1.2rem}
.panel h2{font-size:.82rem;color:var(--mut);text-transform:uppercase;letter-spacing:.04em;margin:0 0 .7rem}
.charts{display:grid;grid-template-columns:1fr 1fr;gap:1.2rem}
.chart{width:100%}
/* uPlot dark-theme overrides */
.u-legend{font-size:.78rem;color:var(--mut)}.u-legend .u-marker{width:.8rem;height:.8rem}
.u-legend .u-value{color:var(--fg)}.u-legend tr.u-series{cursor:pointer}
.u-axis{color:var(--mut)}.uplot{font-family:inherit}
@media(max-width:980px){.cards{grid-template-columns:repeat(2,1fr)}}
@media(max-width:760px){.charts{grid-template-columns:1fr}.cards{grid-template-columns:1fr}}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{padding:.45rem .6rem;text-align:right;border-bottom:1px solid var(--ln)}
th{color:var(--mut);font-weight:500;font-size:.8rem}th:nth-child(2),td:nth-child(2){text-align:left}
tr.cur td{background:rgba(74,144,217,.12)}tr.out td{color:var(--mut);opacity:.5}
.bar{height:.55rem;background:var(--ac);border-radius:3px;display:inline-block;vertical-align:middle}
.flo{display:flex;align-items:center;gap:.6rem;margin-bottom:.8rem;color:var(--mut);font-size:.85rem}
input[type=range]{accent-color:var(--ac)}.tag{color:var(--ok);font-size:.8rem;margin-left:.4rem}
.empty{color:var(--mut);text-align:center;padding:1.6rem}
tbody tr{cursor:pointer}tbody tr:hover td{background:rgba(255,255,255,.04)}
tr.sel td{box-shadow:inset 0 0 0 1px var(--ac)}tr.sel.cur td{box-shadow:inset 0 0 0 1px var(--ac)}
.foc{color:var(--ac);font-weight:600;font-size:.74rem;text-transform:none;letter-spacing:0;margin-left:.3rem}
.legend span{cursor:pointer}.legend span.sel{color:var(--ac)}
.tabs{display:flex;gap:.4rem;padding:.9rem 1.4rem 0;max-width:1100px;margin:0 auto}
.tab{padding:.5rem 1.1rem;border:1px solid var(--ln);border-bottom:none;border-radius:9px 9px 0 0;
  background:var(--card);color:var(--mut);cursor:pointer;font-size:.92rem;font-weight:600;user-select:none}
.tab.on{color:var(--fg);background:var(--bg);box-shadow:inset 0 -2px 0 var(--ac)}
.tab .h{font-size:.72rem;color:var(--mut);font-weight:400;margin-left:.45rem}
.est{font-size:.62rem;color:var(--warn);border:1px solid var(--warn);border-radius:4px;padding:0 .25rem;
  vertical-align:middle;text-transform:uppercase;letter-spacing:.03em;margin-left:.3rem;opacity:.85}
.note{color:var(--mut);font-size:.8rem;line-height:1.55;margin:.2rem 0 1.4rem;
  border-top:1px solid var(--ln);padding-top:.8rem}
.note code{background:var(--card);border:1px solid var(--ln);border-radius:4px;padding:0 .3rem}
</style>
<header>
  <span class=dot id=dot></span>
  <img src="/vendor/nishiki-logo.png" alt="" height=26 style="vertical-align:middle">
  <h1>Nishiki — KOI dashboard</h1>
  <span class=spacer></span><span id=upd>connecting…</span>
</header>
<div class=tabs>
  <div class=tab id=tabM onclick="setView('measured')">Measured run <span class=h>first measurement</span></div>
  <div class=tab id=tabL onclick="setView('live')">Live <span class=h>per call, as your agent runs</span></div>
</div>
<main>
  <div class=cards>
    <div class=card><div class=k>Cumulative cost <span class=est>est.</span><span class=foc id=focA></span></div><div class=v id=cost>$0</div><div class=s id=costs>—</div></div>
    <div class=card><div class=k>Average KOI <span class=est>est.</span><span class=foc id=focB></span></div><div class=v id=koi>—</div><div class=s id=kois>KOI = KPI ÷ $/item</div></div>
    <div class=card><div class=k>Latency<span class=foc id=focC></span></div><div class=v id=lat>—</div><div class=s id=lats>separate axis · not in KOI</div></div>
    <div class=card><div class=k id=ncard>Calls</div><div class=v id=n>0</div><div class=s id=route>—</div></div>
  </div>
  <div class=charts>
    <div class=panel><h2 id=ckoiH>KOI over calls — all candidates compared (selected highlighted)</h2>
      <div id=ckoi class=chart></div></div>
    <div class=panel><h2 id=ccostH>Cumulative cost ($) — what each model would have cost</h2>
      <div id=ccost class=chart></div></div>
  </div>
  <div class=panel>
    <h2>Leaderboard — KOI = <span id=kpilbl>KPI</span> ÷ $/item (no model calls)</h2>
    <div class=flo><span>KPI floor</span><input id=flo type=range min=0 max=1 step=0.05 value=0>
      <span id=floval>0%</span><span style=flex:1></span><span id=best></span></div>
    <table><thead><tr><th>#</th><th>model</th><th>KOI</th><th></th><th>vs baseline</th><th>KPI</th><th>$/item</th><th>latency p50</th></tr></thead>
      <tbody id=tb></tbody></table>
  </div>
  <p class=note id=note></p>
</main>
<script>
const $=id=>document.getElementById(id);
let calls=[], measured=[], snap=null, floor=0, view='live';
const money=x=>x==null?'—':'$'+(x<0.01?x.toFixed(5):x.toFixed(4));
const koifmt=x=>x==null?'—':(x>=10000?(x/1000).toFixed(0)+'k':x.toFixed(0));
const pc=x=>x==null?'—':(x*100).toFixed(0)+'%';
const ms=x=>x==null?'—':(x>=1000?(x/1000).toFixed(1)+'s':Math.round(x)+'ms');
const PALETTE=['#4a90d9','#3fb950','#d29922','#bc8cff','#f778ba','#56d4dd','#e6679a','#f0883e'];
const modelsList=()=>snap?snap.rows.map(r=>r.model):[];
const colorOf=m=>{const i=modelsList().indexOf(m);return PALETTE[(i<0?0:i)%PALETTE.length];};
const P=()=>view==='live'?calls:measured;            // active points for the current tab (same widgets, swapped data)
const curModel=()=>{const C=P();return C.length?C[C.length-1].model:(snap?snap.reference:null);};
// Leaderboard rows for the active tab. Measured tab → the run's rows (static). Live tab → recompute every
// model's $/item (rolling avg of its live calls) and KOI (= measured KPI ÷ that $), so ALL models update &
// re-rank as calls arrive. KPI stays the measured value (no scoring happens live). No live call for a model
// yet → keep its measured numbers.
function rowsView(){
  if(!snap)return [];
  if(view!=='live'||!calls.length)return snap.rows;
  const avg=m=>{const xs=calls.map(c=>(c.per&&c.per[m])?c.per[m].cost:null).filter(v=>v!=null);
    return xs.length?xs.reduce((a,b)=>a+b,0)/xs.length:null;};
  const ref=snap.reference, refR=snap.rows.find(r=>r.model===ref)||{}, refC=avg(ref);
  const refKOI=(refR.kpi!=null&&refC)?refR.kpi/refC:refR.koi;
  return snap.rows.map(r=>{const lc=avg(r.model);
    if(lc==null)return r;
    const koi=(r.kpi!=null)?r.kpi/lc:r.koi;
    const vs=(koi&&refKOI)?koi/refKOI:r.vs_reference;
    return {...r,cost_per_item:lc,koi,vs_reference:vs,live:true};});
}
function bestModel(){if(!snap)return null;
  const s=rowsView().filter(r=>r.kpi!=null&&r.kpi>=floor).sort((a,b)=>(b.koi||0)-(a.koi||0));
  return s.length?s[0].model:null;}
let selected=null, userPicked=false;          // the model the cards/chart focus on (click to change)
const focusModel=()=>selected||bestModel()||curModel();
function pick(m){selected=m;userPicked=true;redraw();}
function sumPer(m,key){let s=0;P().forEach(c=>{const p=c.per&&c.per[m];if(p&&p[key]!=null)s+=p[key];});return s;}
function setView(v){if(v===view)return;view=v;selected=null;userPicked=false;
  try{location.hash=v;}catch(e){}
  syncTabs();redraw();}
function syncTabs(){
  $('tabM').classList.toggle('on',view==='measured');$('tabL').classList.toggle('on',view==='live');
  const live=view==='live';
  $('ncard').textContent=live?'Calls':'Items';
  $('ckoiH').textContent=live?'Running KOI over calls — KPI ÷ avg cost, all candidates (converges to the leaderboard)'
                              :'Running KOI over the measured items — per candidate (converges to the measured KOI)';
  $('ccostH').textContent=live?'Cumulative cost ($) — what each model would have cost'
                              :'Cumulative cost ($) over the measured run — per candidate';
}

function metrics(){
  if(!snap)return;const C=P();const live=view==='live';const RV=rowsView();const sel=focusModel(),cur=curModel();
  const selR=RV.find(r=>r.model===sel)||{}, curR=RV.find(r=>r.model===cur)||{};
  const unit=live?'call':'item';
  const peer=live?'active route':'baseline';                   // what `cur` is: the live route, or the run's reference
  const lbl=sel?sel+(sel===cur?` · ${peer}`:''):'';
  $('focA').textContent=lbl;$('focB').textContent=lbl;$('focC').textContent=lbl;
  // cost — what `sel` would have cost over the points so far
  const selCost=sumPer(sel,'cost'), curCost=sumPer(cur,'cost');
  $('cost').textContent=money(selCost);
  if(sel===cur)$('costs').innerHTML=C.length?`<b>${money(selCost/C.length)}</b> avg/${unit}${live?' ('+peer+')':' (measured)'}`:`— no ${unit}s yet`;
  else{const d=curCost>0?(curCost-selCost)/curCost*100:0;
    $('costs').innerHTML=`vs ${peer} ${cur}: <b>${d>=0?'−':'+'}${Math.abs(d).toFixed(0)}%</b> (was ${money(curCost)})`;}
  // KOI — the aggregate KOI = KPI ÷ avg cost, IDENTICAL to this model's leaderboard row (no ratio-averaging,
  // which would blow up on cheap calls). selR comes from rowsView() so it's live (rolling) or measured.
  $('koi').textContent=koifmt(selR.koi);
  const x=(selR.koi&&curR.koi)?selR.koi/curR.koi:null;
  $('kois').innerHTML=(sel===cur)?peer:`vs ${peer}${x?` · <b>${x.toFixed(1)}x</b>`:''}`;
  // latency (separate axis) — live avg for the active route, else measured p50
  let latv,note;
  if(live&&sel===cur){const ls=C.map(c=>c.latency_ms).filter(v=>v!=null);
    if(ls.length){latv=ls.reduce((a,b)=>a+b,0)/ls.length;note=`live avg · measured p95 ${ms(curR.latency_ms_p95)}`;}
    else{latv=curR.latency_ms_p50;note='measured p50';}}
  else{latv=selR.latency_ms_p50;note=`measured p50 · p95 ${ms(selR.latency_ms_p95)}`;}
  $('lat').textContent=ms(latv);$('lats').innerHTML=note;
  // count
  $('n').textContent=C.length;$('route').textContent=(live?'active route (last call): ':'baseline: ')+(cur||'—');
}

function buildSeries(metric){
  if(!snap)return [];const C=P();const RV=rowsView();const sel=focusModel(),cur=curModel();
  const pass=RV.filter(r=>r.kpi!=null&&r.kpi>=floor).sort((a,b)=>(b.koi||0)-(a.koi||0));
  const top=pass.slice(0,5);                                   // compare the top 5 contenders…
  [sel,cur].forEach(m=>{if(m&&!top.some(r=>r.model===m)){       // …always including the selected & current
    const rr=RV.find(r=>r.model===m);if(rr)top.push(rr);}});
  const kpiOf=m=>(snap.rows.find(x=>x.model===m)||{}).kpi;       // measured KPI (rate) for running KOI
  return top.map(r=>{const m=r.model;let vals;
    if(metric==='koi'){
      if(view==='live'){const k=kpiOf(m);let cum=0,n=0;          // running KOI = KPI ÷ running avg cost
        vals=C.map(c=>{const p=c.per&&c.per[m];if(p&&p.cost!=null){cum+=p.cost;n++;}
          return (k!=null&&n>0&&cum>0)?k/(cum/n):null;});}
      else vals=C.map(c=>(c.per&&c.per[m])?c.per[m].koi:null);    // measured: per[].koi is already running KOI
    }
    else{let run=0;vals=C.map(c=>{const p=c.per&&c.per[m];if(p&&p.cost!=null)run+=p.cost;return run;});}
    return {name:m,vals,color:colorOf(m),emph:m===sel};});
}

const _u={};                                                   // id -> {plot, sig} live uPlot instances
const cw=el=>Math.max(220,((el.parentElement&&el.parentElement.clientWidth)||460)-38);
function makeChart(id,series,fmt){
  const el=$(id);const C=P();
  if(!C.length){if(_u[id]){_u[id].plot.destroy();delete _u[id];}
    el.innerHTML='<div style="color:#8b98a5;text-align:center;padding:3rem 0;font-size:.85rem">'
      +(view==='live'?'no calls yet — run your agent':'no per-item detail in this run')+'</div>';return;}
  const x=C.map((_,i)=>i+1);
  const data=[x,...series.map(s=>s.vals)];
  const sig=(view+'|')+series.map(s=>s.name+(s.emph?'*':'')).join('|');  // recreate when tab/series/emphasis changes
  const w=cw(el);
  if(_u[id]&&_u[id].sig===sig){_u[id].plot.setData(data);_u[id].plot.setSize({width:w,height:150});return;}
  if(_u[id])_u[id].plot.destroy();else el.innerHTML='';
  const xlabel=view==='live'?'call #':'item #';
  const opts={width:w,height:150,padding:[10,10,0,0],
    legend:{live:true},cursor:{points:{size:7}},scales:{x:{time:false}},
    axes:[{stroke:'#8b98a5',grid:{stroke:'#222c38'},ticks:{stroke:'#222c38'}},
          {stroke:'#8b98a5',grid:{stroke:'#222c38'},ticks:{stroke:'#222c38'},size:58,values:(u,vs)=>vs.map(fmt)}],
    series:[{label:xlabel},...series.map(s=>({label:s.name,stroke:s.color,width:s.emph?3:1.5,
      points:{show:false},value:(u,v)=>v==null?'—':fmt(v)}))]};
  _u[id]={plot:new uPlot(opts,data,el),sig};
}

function charts(){
  if(typeof uPlot==='undefined')return;
  makeChart('ckoi',buildSeries('koi'),koifmt);
  makeChart('ccost',buildSeries('cost'),money);
}
window.addEventListener('resize',()=>{for(const id in _u)_u[id].plot.setSize({width:cw($(id)),height:150});});

function leaderboard(){
  if(!snap)return;
  const KL=snap.kpi_name||'KPI';$('kpilbl').textContent=KL;
  const liveVals=view==='live'&&calls.length;                  // are $/item & KOI rolling off live calls?
  const ref=snap.reference, cur=curModel(), best=bestModel(), sel=focusModel();
  const all=rowsView().map(r=>({...r,surv:(r.kpi!=null&&r.kpi>=floor)}));
  const surv=all.filter(r=>r.surv).sort((a,b)=>(b.koi||0)-(a.koi||0));
  const out=all.filter(r=>!r.surv).sort((a,b)=>(b.kpi||0)-(a.kpi||0));
  const mx=Math.max(...surv.map(r=>r.koi||0),1);
  $('best').innerHTML=(surv.length?`★ best: <b>${surv[0].model}</b> · ${money(surv[0].cost_per_item)}/item · ${ms(surv[0].latency_ms_p50)}`:'⚠ none pass the floor')
    +(liveVals?` <span class=tag>live: $/item & KOI rolling over ${calls.length} call${calls.length>1?'s':''} (KPI = measured)</span>`:'');
  const cls=r=>[r.model===cur?'cur':'',r.model===sel?'sel':''].filter(Boolean).join(' ');
  let h='';
  surv.forEach((r,i)=>{const isc=r.model===cur;
    h+=`<tr class="${cls(r)}" onclick="pick('${r.model}')"><td>${i+1}</td><td>${isc?'▸ ':''}${r.model}${r.model===ref?' (baseline)':''}${r.model===best?' <span class=tag>★ best</span>':''}</td>`
     +`<td><b>${koifmt(r.koi)}</b></td><td style=text-align:left><span class=bar style=width:${((r.koi||0)/mx*90).toFixed(0)}px></span></td>`
     +`<td>${r.vs_reference==null?'—':r.vs_reference.toFixed(1)+'x'}</td><td>${pc(r.kpi)}</td><td>${money(r.cost_per_item)}</td><td>${ms(r.latency_ms_p50)}</td></tr>`;});
  out.forEach(r=>{h+=`<tr class="out ${cls(r)}" onclick="pick('${r.model}')"><td>✗</td><td>${r.model}</td><td colspan=2>cut by floor</td>`
     +`<td>—</td><td>${pc(r.kpi)}</td><td>${money(r.cost_per_item)}</td><td>${ms(r.latency_ms_p50)}</td></tr>`;});
  $('tb').innerHTML=h||'<tr><td colspan=8 class=empty>no candidates</td></tr>';
}

function footer(){
  const base="Costs &amp; KOI are <b>estimates</b> — token counts × MODELS.yaml prices, not your provider's bill. "
    +"Check your provider's console for exact charges.";
  const scope=view==='live'
    ? ` Live: cumulative over <b>${calls.length}</b> call${calls.length===1?'':'s'} in this profile's `
      +`<code>.nishiki/live.jsonl</code> (reset with <code>nishiki run --fresh</code>).`
    : ` Measured: from the latest scored run (<b>${measured.length}</b> item${measured.length===1?'':'s'}).`;
  $('note').innerHTML=base+scope;
}
function redraw(){if(!userPicked)selected=bestModel();metrics();charts();leaderboard();footer();}

$('flo').addEventListener('input',e=>{floor=+e.target.value;$('floval').textContent=pc(floor);redraw();});

const es=new EventSource('/events');
es.addEventListener('snapshot',e=>{
  snap=JSON.parse(e.data);calls=snap.calls||[];measured=snap.measured||[];
  floor=snap.kpi_floor||0;$('flo').value=floor;$('floval').textContent=pc(floor);
  const h=(location.hash||'').replace('#','');                  // deterministic deep-link: #measured / #live
  view=(h==='measured'||h==='live')?h:(calls.length?'live':(measured.length?'measured':'live'));
  $('dot').classList.add('on');syncTabs();redraw();
});
es.addEventListener('call',e=>{const c=JSON.parse(e.data);
  if(!calls.some(x=>x.i===c.i)){calls.push(c);calls.sort((a,b)=>a.i-b.i);if(view==='live')redraw();}
  $('upd').textContent='updated '+new Date().toLocaleTimeString();});
es.addEventListener('ping',e=>{$('upd').textContent='live · '+JSON.parse(e.data).t;});
es.onerror=()=>{$('dot').classList.remove('on');$('upd').textContent='disconnected — reconnecting…';};
window.addEventListener('hashchange',()=>{const h=(location.hash||'').replace('#','');
  if((h==='measured'||h==='live')&&h!==view)setView(h);});
</script>
</html>"""
