"""KOI optimal-table HTML generation (interactive-slider version) — from a scored-run JSON.

Separate from report.py (for the mock/standard pipeline). Reads the glue's output schema
(candidates[].overall_acc / ng_recall / ng_total / ng_miss_count / cost_per_item / a per-item list …) and
emits HTML where **the floors can be moved dynamically with browser sliders** (KOI.yaml's floors as initial values).
Survivors are sorted descending by KOI = match rate ÷ cost; disqualified ones are grayed out.

Usage: nishiki koi-report --experiment <DIR> [--result runs/x.json] [--out koi_report.html]
"""
import html as _html
import json

from . import koi_estimate


def _floors(koi_yaml_path):
    """KOI.yaml floors (kpi_floor / ng_recall_floor) = slider initial values. Defaults if absent."""
    fk, fn = 0.8, 0.7
    try:
        import yaml
        d = yaml.safe_load(open(koi_yaml_path, encoding="utf-8")) or {}
        f = d.get("floors") or {}
        fk = f.get("kpi_floor", fk)
        fn = f.get("ng_recall_floor", fn)
    except Exception:  # noqa: BLE001
        pass
    return float(fk), float(fn)


def _fa_rate(c):
    """False-alarm rate (fraction judged NG when gold≠NG) from the per-item list. None if absent."""
    pv = koi_estimate.per_item_list(c)
    if not pv:
        return None
    non_ng = [v for v in pv if v.get("gold") != "NG"]
    if not non_ng:
        return None
    fp = sum(1 for v in non_ng if v.get("pred") == "NG")
    return fp / len(non_ng)


def _rows(payload):
    rows = []
    for c in payload.get("candidates", []):
        ng_total = c.get("ng_total") or 0
        ng_hit = ng_total - (c.get("ng_miss_count") or 0)
        rows.append({
            "label": c.get("label", "?"),
            "ref": bool(c.get("is_reference")),
            "kpi": c.get("overall_acc"),               # match rate (0-1)
            "ng_recall": c.get("ng_recall"),
            "ng_total": ng_total, "ng_hit": ng_hit,
            "fa_rate": _fa_rate(c),
            "cpi": c.get("cost_per_item") or 0.0,
            "errors": c.get("errors", 0),
            "p50": c.get("latency_ms_p50"),     # latency (separate axis): None = an old run not measured
            "p95": c.get("latency_ms_p95"),
        })
    return rows


def generate(result_json, koi_yaml, out_html):
    with open(result_json, encoding="utf-8") as f:
        payload = json.load(f)
    fk, fn = _floors(koi_yaml)
    rows = _rows(payload)
    # Baseline for current-model comparison: the is_reference candidate (null if absent)
    ref = next((r for r in rows if r["ref"] and r["cpi"] and r["kpi"] is not None), None)
    refkoi = (ref["kpi"] / ref["cpi"]) if ref else None
    # Is this a classification task (= some candidate has the NG dimension)? Non-classification
    # (CUAD extraction etc.) disables the NG floor and hides the slider. Generalize the KPI label
    # too (classification = match rate / non-classification = KPI).
    has_ng = any(r["ng_recall"] is not None for r in rows)
    kpi_label = "match rate" if has_ng else "KPI"

    data = json.dumps({
        "gold_batch": payload.get("gold_batch"), "n": payload.get("n"),
        "gold_dist": payload.get("gold_dist"),
        "full_total": payload.get("full_run_total_est"),
        "fk": fk, "fn": fn, "refkoi": refkoi, "rows": rows,
        "has_ng": has_ng, "kpi_label": kpi_label,
    }, ensure_ascii=False)

    doc = """<!doctype html><html lang=en><meta charset=utf-8><title>KOI optimal table</title>
<style>body{font:14px/1.6 system-ui,sans-serif;margin:2rem;color:#222}h1{font-size:1.3rem}
.ctl{background:#f7f7f9;border:1px solid #ddd;border-radius:8px;padding:1rem;max-width:600px}
.ctl .row{margin:.4rem 0}.ctl label{display:inline-block;width:8rem}
input[type=range]{vertical-align:middle;width:240px}.v{display:inline-block;width:3rem;text-align:right;font-variant-numeric:tabular-nums}
.best{background:#e8f5e9;border:1px solid #81c784;border-radius:8px;padding:.7rem 1rem;margin:1rem 0;max-width:600px;font-size:1.05rem}
table{border-collapse:collapse;margin-top:1rem}th,td{border:1px solid #ccc;padding:.35rem .6rem;text-align:right}
th:nth-child(2),td:nth-child(2){text-align:left}tr.ref{background:#fff3cd}tr.out{color:#aaa;background:#fafafa}
.bar{height:.7rem;background:#4a90d9;border-radius:2px;display:inline-block;vertical-align:middle}
.note{color:#666;font-size:.85rem;margin-top:1rem;max-width:680px}button{margin-left:1rem}</style>
<h1>KOI optimal table — gold batch <span id=gb></span> (n=<span id=nn></span>)</h1>
<p id=meta></p>
<div class=ctl><b>Floors (SLA) — drag to choose dynamically</b>
 <div class=row><label><span id=lblk>match rate</span> floor</label><input id=fk type=range min=0 max=1 step=0.05><span class=v id=lfk></span>
   <button id=reset>Reset to defaults</button></div>
 <div class=row id=ngrow><label>NG-detection floor</label><input id=fn type=range min=0 max=1 step=0.05><span class=v id=lfn></span></div>
 <div class=row style=color:#666>Candidates that fail are disqualified (grayed bottom rows). Survivors sorted descending by KOI = match rate ÷ cost.</div>
</div>
<div class=best id=best></div>
<table id=tbl></table>
<p class=note>KOI = match rate ÷ $/item (passing candidates only; higher = more cost-efficient). Floor = "the must-hit quality line", KOI = "cost efficiency above that line".
Under mode1 (gold = current output) what's measurable is "the same as current, cheaper". Beating current needs mode2 (human labels).
<b>When the top contenders are close (a few NG apart) ranks can wobble due to LLM non-determinism, so confirm with multiple runs.</b>
Latency (p50/p95) is a <b>separate axis</b> = not in the KOI formula, shown for reference. Cross-border (OpenRouter etc.) tends to be slower due to RTT.
Latency from a single run is also noisy (varies with load) = use multiple runs to settle ranks. "—" is an old run not measured.</p>
<script>
const D=__PAYLOAD__,$=id=>document.getElementById(id);
gb.textContent=D.gold_batch;nn.textContent=D.n;
meta.textContent=`gold dist ${JSON.stringify(D.gold_dist||{})}`+(D.full_total?` · est. scored-run total $${D.full_total}`:'');
const pc=x=>x==null?'—':(x*100).toFixed(0)+'%';
const ms=x=>x==null?'—':(x/1000).toFixed(1)+'s';   // latency (separate axis). shown in seconds
function recompute(){
  const fk=+$('fk').value, fn=+$('fn').value, KL=D.kpi_label||'match rate';
  $('lfk').textContent=(fk*100).toFixed(0)+'%';$('lfn').textContent=(fn*100).toFixed(0)+'%';
  const all=D.rows.map(d=>({...d, koi:(d.cpi&&d.kpi!=null)?d.kpi/d.cpi:0,
    surv:(d.kpi!=null&&!d.errors&&d.kpi>=fk&&(d.ng_recall==null||d.ng_recall>=fn))}));
  const surv=all.filter(r=>r.surv).sort((a,b)=>b.koi-a.koi);
  const out=all.filter(r=>!r.surv).sort((a,b)=>(b.kpi||0)-(a.kpi||0));
  const mx=Math.max(...surv.map(r=>r.koi),1);
  if(surv.length){const w=surv[0];const rel=D.refkoi?` · vs current ${(w.koi/D.refkoi).toFixed(1)}x`:'';
    const ngp=(D.has_ng!==false)?` · NG detection ${pc(w.ng_recall)}`:'';
    $('best').innerHTML=`★ Recommended path: <b>${w.label}</b> ${KL} ${pc(w.kpi)}${ngp} · $${w.cpi.toFixed(5)}/item${rel}`;}
  else $('best').innerHTML='⚠ No candidate meets the floors (floor too high or all candidates below quality)';
  let h=`<tr><th>#</th><th>model</th><th>KOI</th><th></th><th>vs current</th><th>${KL}</th><th>NG detection</th><th>false alarm</th><th>$/item</th><th>latency p50</th><th>p95</th></tr>`;
  surv.forEach((r,i)=>{const rel=D.refkoi?(r.koi/D.refkoi).toFixed(1)+'x':'-';
    h+=`<tr class="${r.ref?'ref':''}"><td>${i+1}</td><td>${r.label}${r.ref?' (current)':''}</td><td><b>${r.koi.toFixed(0)}</b></td>`
      +`<td style=text-align:left><span class=bar style=width:${(r.koi/mx*120).toFixed(0)}px></span></td>`
      +`<td>${rel}</td><td>${pc(r.kpi)}</td><td>${r.ng_hit}/${r.ng_total} (${pc(r.ng_recall)})</td>`
      +`<td>${r.fa_rate==null?'—':pc(r.fa_rate)}</td><td>$${r.cpi.toFixed(5)}</td><td>${ms(r.p50)}</td><td>${ms(r.p95)}</td></tr>`;});
  out.forEach(r=>{const bad=[];if(r.errors)bad.push('errors'+r.errors);
    if(r.kpi!=null&&r.kpi<fk)bad.push('match'+pc(r.kpi));if(r.ng_recall!=null&&r.ng_recall<fn)bad.push('NG'+pc(r.ng_recall));
    h+=`<tr class=out><td>✗</td><td>${r.label}</td><td colspan=2>cut by floor (${bad.join(' / ')||'not measurable'})</td>`
      +`<td>-</td><td>${pc(r.kpi)}</td><td>${r.ng_hit}/${r.ng_total} (${pc(r.ng_recall)})</td>`
      +`<td>${r.fa_rate==null?'—':pc(r.fa_rate)}</td><td>$${r.cpi.toFixed(5)}</td><td>${ms(r.p50)}</td><td>${ms(r.p95)}</td></tr>`;});
  $('tbl').innerHTML=h;
}
$('fk').value=D.fk;$('fn').value=D.fn;
$('lblk').textContent=D.kpi_label||'match rate';
if(D.has_ng===false)$('ngrow').style.display='none';   // non-classification = hide the NG slider
['fk','fn'].forEach(id=>$(id).addEventListener('input',recompute));
$('reset').onclick=()=>{$('fk').value=D.fk;$('fn').value=D.fn;recompute();};
recompute();
</script></html>""".replace("__PAYLOAD__", data)
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(doc)
    # Return the recommendation (at the initial floors) for the terminal. Non-classification (ng_recall=None) passes the NG floor.
    surv = [r for r in rows if r["kpi"] is not None and not r["errors"] and r["cpi"]
            and r["kpi"] >= fk and (r["ng_recall"] is None or r["ng_recall"] >= fn)]
    surv.sort(key=lambda r: -(r["kpi"] / r["cpi"]))
    return out_html, (surv[0]["label"] if surv else None)
