"""Nishiki runner — task-agnostic generic runner shell (design doc §18.9, 2026-06-22).

A task-agnostic extraction of the example_target glue "shell" (candidate resolution, per-item loop,
probe/run modes, scoring aggregation, run-JSON output). A **locally-executed adapter** (a self-built
agent such as CUAD) imports it and plugs in four hooks; then KOI can be measured by adding only an
adapter per target (proof of generalization).

  Note: the example_target container glue uses a stdin pipe (no package import), so **this cannot be
  used there**. The scoring semantics, however, are reproduced with identical numbers by
  `scoring.verdict_extras` (the single source of truth for the contract).

The contract an adapter satisfies (duck typing, no forced base class):
  catalog   : {key: {"model_id","in","out","quality_hint"?,"note"?,"call"?}}  candidate facts
  known     : list[str]                                 known (current) candidate keys
  labels    : list[str] | None                          classification labels (None=non-classification scalar)
  positive  : str | None                                positive class (classification only. e.g. "NG")
  reference : str | None                                baseline candidate for current comparison (e.g. "CASCADE")
  load_items() -> list[{"id","gold",...}]               scoring items (read-only)
  set_model(cand) -> None                               swap the model to cand at run time
  run_item(item) -> (pred, cost, latency_s, err)        hit the choke once (billing happens only here)
  score_item(pred, gold) -> float(0..1)                 per-item scalar score
"""
from __future__ import annotations

import json
import sys

from . import scoring


def resolve_candidates(spec, catalog, known):
    """Resolve an NZ_CANDIDATES-style spec (ALL/NEW/KNOWN/CASCADE/comma-separated) into a list of candidate keys.

    Same semantics as the example_target glue's resolve_candidates (order-preserving, dedup).
    """
    new = [k for k in catalog if k not in known]
    out = []
    for tok in (spec or "ALL").split(","):
        tok = tok.strip()
        if tok == "ALL":
            out += list(catalog)
        elif tok == "NEW":
            out += new
        elif tok == "KNOWN":
            out += list(known)
        elif tok == "CASCADE" or tok in catalog:
            out.append(tok)
    seen, uniq = set(), []
    for c in out:
        if c not in seen:
            seen.add(c); uniq.append(c)
    return uniq


def run_candidate(adapter, cand, items, *, verbose=True):
    """Run candidate cand over all items, returning {id: {pred, cost, latency, score}} plus total cost and error count.

    Exceptions are caught per item (pred=None, errs+1) and do not stop the run (same robustness as the example_target glue).
    """
    adapter.set_model(cand)
    res, cost_sum, errs = {}, 0.0, 0
    for it in items:
        latency = 0.0
        try:
            pred, cost, latency, err = adapter.run_item(it)
            if err:
                errs += 1
        except Exception as e:  # noqa: BLE001
            if verbose:
                print(f"  ! {it['id']} {cand}: {str(e)[:120]}", file=sys.stderr)
            pred, cost, errs = None, 0.0, errs + 1
        score = adapter.score_item(pred, it["gold"]) if pred is not None else 0.0
        res[it["id"]] = {"pred": pred, "cost": cost, "latency": latency, "score": score}
        cost_sum += cost
        if verbose:
            ms = f"{latency*1000:.0f}ms" if latency else "-"
            print(f"  {it['id']} {cand}: score={score:.2f} ${cost:.4f} {ms}", file=sys.stderr)
    return res, cost_sum, errs


def _score_candidate(adapter, cand, items, res, cost_sum, errs):
    """Build the run-JSON dict for one candidate (generic KPI + extra dimensions if classification + latency + per-item).

    overall_acc = kpi (= mean score) is always set = the slot koi_report/suggest_floor reads.
    Classification adapters (with labels) add ng_recall / macro-F1 (non-classification adds none = floors=kpi only).
    """
    gold_map = {it["id"]: it["gold"] for it in items}
    per_item = [{"id": it["id"], "gold": it["gold"],
                 "pred": res[it["id"]]["pred"], "score": round(res[it["id"]]["score"], 4),
                 "cost": round(res[it["id"]]["cost"], 6),
                 "latency_ms": round(res[it["id"]]["latency"] * 1000, 1)}
                for it in items]
    kpi = scoring.aggregate_kpi([{"score": p["score"]} for p in per_item])
    cpi = cost_sum / len(items) if items else 0.0
    lats = [res[it["id"]]["latency"] * 1000 for it in items if res[it["id"]]["latency"]]
    lp50, lp95, lmean = scoring.latency_stats(lats)

    c = {"label": cand, "is_reference": cand == getattr(adapter, "reference", None),
         "n": len(items), "kpi": kpi, "overall_acc": kpi,
         "cost_total": round(cost_sum, 6), "cost_per_item": round(cpi, 6), "errors": errs,
         "koi_kpi": (kpi * 100.0 / (cpi * 1000)) if cpi else None,
         "latency_ms_p50": round(lp50, 1) if lp50 is not None else None,
         "latency_ms_p95": round(lp95, 1) if lp95 is not None else None,
         "latency_ms_mean": round(lmean, 1) if lmean is not None else None,
         "per_item": per_item}

    labels = getattr(adapter, "labels", None)
    if labels:                                  # classification task = extra dimensions (NG recall etc.)
        pred_map = {it["id"]: res[it["id"]]["pred"] for it in items}
        extras = scoring.verdict_extras(gold_map, pred_map, labels,
                                        getattr(adapter, "positive", None))
        c.update(extras)
        c["overall_acc"] = extras["overall_acc"]   # classification uses exact-match rate as KPI (= matches kpi)
    else:                                       # non-classification = ng_* absent (floors is kpi_floor only)
        c["ng_recall"] = None
        c["ng_total"] = 0
        c["ng_miss_count"] = 0
    return c


def mode_run(adapter, cands, *, verbose=True):
    """Scored run. Returns a run-JSON dict (the caller saves it to runs/ → koi-report)."""
    items = adapter.load_items()
    if verbose:
        print(f"[run] in-scope={len(items)} items", file=sys.stderr)
    out = {"mode": "run", "n": len(items), "candidates": []}
    for cand in cands:
        res, cost_sum, errs = run_candidate(adapter, cand, items, verbose=verbose)
        c = _score_candidate(adapter, cand, items, res, cost_sum, errs)
        out["candidates"].append(c)
        if verbose:
            rr = "-" if c.get("ng_recall") is None else f"{c['ng_recall']:.0%}"
            print(f"[{cand}] KPI {c['kpi']:.2f}  NGrec {rr}  "
                  f"${c['cost_total']:.4f}(${c['cost_per_item']:.5f}/item) err={errs}",
                  file=sys.stderr)
    # KPI descending → cost ascending (quality delivered cheapest first)
    out["candidates"].sort(key=lambda x: (-(x["kpi"] or -1), x["cost_per_item"]))
    return out


def mode_probe(adapter, cands, probe_n, *, verbose=True):
    """probe (low-billing preview). Run each candidate on only the first probe_n items and project cost to the full set."""
    items = adapter.load_items()
    n_total = len(items)
    sample = items[:probe_n]
    if verbose:
        print(f"[probe] in-scope={n_total} / trial run of {len(sample)} items per candidate", file=sys.stderr)
    out = {"mode": "probe", "n_inscope": n_total, "probe_n": len(sample), "candidates": []}
    for cand in cands:
        res, cost_sum, errs = run_candidate(adapter, cand, sample, verbose=verbose)
        per_item = cost_sum / len(sample) if sample else 0.0
        none_cnt = sum(1 for r in res.values() if r["pred"] is None)
        lats = [r["latency"] * 1000 for r in res.values() if r["latency"]]
        lp50, _, _ = scoring.latency_stats(lats)
        out["candidates"].append({
            "label": cand, "probe_cost": round(cost_sum, 6), "per_item": round(per_item, 6),
            "proj_full": round(per_item * n_total, 4), "errors": errs, "pred_none": none_cnt,
            "latency_ms_p50": round(lp50, 1) if lp50 is not None else None})
    out["candidates"].sort(key=lambda x: x["proj_full"])
    out["probe_spend_total"] = round(sum(c["probe_cost"] for c in out["candidates"]), 4)
    out["full_run_total_est"] = round(sum(c["proj_full"] for c in out["candidates"]), 4)
    return out


def emit(out):
    """Print the run/probe-JSON to stdout on one line (the orchestrator saves it to runs/)."""
    print(json.dumps(out, ensure_ascii=False))
