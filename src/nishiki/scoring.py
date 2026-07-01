"""Nishiki scoring — task-independent scalar KPI scoring + extra dimensions for classification tasks (design doc §18.9).

2026-06-22 Core of non-classification KPI generalization. To measure targets beyond
example_target (classification = verdict match), generalize scoring from "verdict match"
to a **scalar KPI (0..1)**.

Generic contract:
  per-item result = {"id", "pred", "gold", "score": 0..1, ...}
  aggregate KPI   = mean(score)  ← the `overall_acc`/`kpi` slot read by koi_report / suggest_floor

How score is set per task:
  classification (example_target) : score = 1.0 if pred == gold else 0.0 (= match rate). On top of
                         this, `verdict_extras` adds NG recall / macro-F1 as **extra dimensions**.
  non-classification (CUAD extraction etc.) : score = a continuous 0..1 value such as span-overlap F1.
                         No extra dimensions (= floors is kpi_floor only; ng_recall_floor is
                         classification-specific = optional).

This module is pure functions: stdlib only, no side effects. The example_target container glue
is not importable as a package (stdin pipe) so it can't be used directly, but its classification
semantics are reproduced here with identical numbers by `verdict_extras` (pinned by regression
tests) = the single source of truth for the contract.
"""
from __future__ import annotations


def latency_stats(values_ms):
    """Return (p50, p95, mean) from a list of milliseconds (no numpy; all None if empty).

    Same algorithm as `_latency_stats` in the example_target glue (contract match).
    """
    xs = sorted(values_ms)
    if not xs:
        return None, None, None

    def pct(p):
        return xs[min(len(xs) - 1, int(p * len(xs)))]

    return pct(0.5), pct(0.95), sum(xs) / len(xs)


def aggregate_kpi(per_item):
    """Return aggregate KPI = mean score from per-item results (each `score` is 0..1).

    Args:
      per_item: [{"score": float|None, ...}, ...]. A None score (= run failure / no pred) is
                **counted as 0** (failures drag the KPI down = no leniency). Items without gold
                must be excluded by the caller (every item here counts toward the denominator).
    Returns:
      Mean score over n items (0..1). 0.0 if empty.

    Guard: a per-item score MUST be a normalized 0..1 value (so the KPI stays a rate ≤100%, which
    the %-display, floors and KOI all assume). Any score outside [0,1] (or non-numeric) is a scorer
    contract violation — it is clamped into [0,1] and a one-line warning is emitted, so a misnamed /
    un-normalized metric (e.g. a raw count) can't silently inflate the KPI past 100%.
    """
    if not per_item:
        return 0.0
    total, bad = 0.0, 0
    for it in per_item:
        s = it.get("score")
        if s is None:
            s = 0.0
        elif not isinstance(s, (int, float)) or s < 0.0 or s > 1.0:
            bad += 1
            s = 0.0 if not isinstance(s, (int, float)) else min(1.0, max(0.0, s))
        total += s
    if bad:
        import sys
        print(f"[nishiki] WARNING: {bad}/{len(per_item)} per-item score(s) were outside [0,1] "
              f"(or non-numeric) and were clamped — a scorer must return a normalized 0..1 score "
              f"so KPI stays a rate (≤100%).", file=sys.stderr)
    return total / len(per_item)


def verdict_extras(gold_map, pred_map, labels, positive):
    """Extra dimensions specific to classification tasks. Computes recall/precision treating the
    `positive` label (e.g. "NG") as positive, plus macro-F1. Produces identical numbers to the
    example_target glue's `score()` (contract match, regression-pinned).

    Args:
      gold_map: {id: gold_label}
      pred_map: {id: pred_label} (a missing key is treated as None = mismatch)
      labels:   list of all class labels (the population for macro-F1; e.g. ["OK","needs review","NG"])
      positive: the safety-critical class (e.g. "NG") = the target for recall/precision/misses
    Returns:
      {overall_acc, ng_recall, ng_precision, ng_miss, ng_miss_count, ng_total, macro_f1}
      Note: key names are backward-compatible with the existing example_target run-JSON (read by koi_report/suggest_floor).
    """
    n = len(gold_map)
    agree = sum(1 for vn, g in gold_map.items() if pred_map.get(vn) == g)

    g_pos = {vn for vn, g in gold_map.items() if g == positive}
    p_pos = {vn for vn, p in pred_map.items() if p == positive}
    tp = len(g_pos & p_pos)
    ng_recall = tp / len(g_pos) if g_pos else None
    ng_prec = tp / len(p_pos) if p_pos else None
    ng_miss = sorted(g_pos - p_pos)

    f1s = []
    for c in labels:
        g_c = {vn for vn, g in gold_map.items() if g == c}
        p_c = {vn for vn, p in pred_map.items() if p == c}
        t = len(g_c & p_c)
        rec = t / len(g_c) if g_c else None
        pre = t / len(p_c) if p_c else None
        f1 = (2 * pre * rec / (pre + rec)) if (pre and rec) else 0.0
        f1s.append(f1)
    macro_f1 = sum(f1s) / len(f1s) if f1s else 0.0

    return {
        "overall_acc": agree / n if n else 0.0,
        "overall_agree": agree,
        "ng_total": len(g_pos), "ng_recall": ng_recall, "ng_precision": ng_prec,
        "ng_miss": ng_miss, "ng_miss_count": len(ng_miss), "macro_f1": macro_f1,
    }


# ───────────────────────── score functions for non-classification tasks (0..1) ───────────────────
def label_match(pred, gold):
    """Per-item score for a classification task = 1.0 on exact match (= the basis of match rate)."""
    return 1.0 if pred == gold else 0.0


def span_overlap_f1(pred_spans, gold_spans):
    """Per-item score for an extraction task = overlap F1 of character-offset sets (0..1).

    For CUAD (span extraction of contract clauses). pred/gold are lists of (start, end) half-open
    intervals (one item may have multiple spans = the same clause appears in several places).
    Measured by set agreement of character positions:
      precision = |pred chars ∩ gold chars| / |pred chars|
      recall    = |pred chars ∩ gold chars| / |gold chars|
      F1        = 2PR/(P+R)
    Both empty (= correctly "none applicable") is 1.0; only one empty is 0.0. Tokenizer-independent and language-neutral.
    """
    def charset(spans):
        s = set()
        for a, b in spans or []:
            if b > a:
                s.update(range(a, b))
        return s

    p, g = charset(pred_spans), charset(gold_spans)
    if not p and not g:
        return 1.0
    if not p or not g:
        return 0.0
    inter = len(p & g)
    if inter == 0:
        return 0.0
    prec = inter / len(p)
    rec = inter / len(g)
    return 2 * prec * rec / (prec + rec)


# ───────────────────────── scoring registry (name → per-item scoring function) ──────────────────
# Table the generic init / generic adapter uses to look up a scoring function from KOI.yaml's
# scorer.kpi (string). To measure "the user's KPI, whether classification or not", scoring logic
# is **chosen from this built-in set** (the AI doesn't generate arbitrary code; it detects the
# kind and assigns an existing scorer = safe).
SCORERS = {
    "label_match": label_match,     # classification: 1/0 on pred==gold (aggregate = match rate)
    "span_f1": span_overlap_f1,     # extraction: character-span overlap F1 (language-neutral)
}

# KPI kind (task_type) → default scorer name. init's auto-detection picks the kind, mapped to a scorer here.
TASK_SCORER = {
    "classification": "label_match",
    "extraction": "span_f1",
}


def get_scorer(name):
    """Scorer name → per-item scoring function. Unknown names raise explicitly (catches KOI.yaml typos early)."""
    try:
        return SCORERS[name]
    except KeyError:
        raise ValueError(f"unknown scorer.kpi='{name}' (supported: {sorted(SCORERS)})") from None
