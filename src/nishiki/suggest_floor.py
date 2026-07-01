"""nishiki suggest-floor — deterministically compute the "failure-band floor" from past run measurements (design doc §18.9).

Goal: the "cheapest N" candidate selection wastes probe budget on the ultra-cheap band that has wiped out in
prior runs (where quality doesn't hold up on Japanese OCR). It proposes `--min-price` based on **the price of
models that fell below the floors in past runs**.

Division of roles (confirmed by the user on 2026-06-21):
  - **Deterministic core (this module)** = from run JSON + KOI floors + MODELS prices, extract the candidates
    that failed and compute the floor value **from numbers only**. No AI quality guessing whatsoever.
  - **AI (orchestrator)** = handles only the **interpretation (proposal text) and the approval dialog** of the
    computed result ("last time nova$0.06/gemma$0.04 failed → narrow to $0.1 and above?").

How the floor is decided (number-derived):
  The cheapest price among passing (floors-satisfying) candidates = `cheapest_pass`. Below this = failure record
  or an unproven ultra-cheap band. Only when there is a candidate that **actually failed** below it (= there is
  evidence of cheap-and-failing) does it propose `cheapest_pass` as `--min-price` (this value itself is excluded
  on the keep side via `<`). A cheap candidate that passes (inversion) lowers cheapest_pass, so it automatically
  stays on the keep side. If there is no failure record, no proposal (None).
"""


def compute_floor(run, floors, prices):
    """Compute the failure-band floor from run (scored-run JSON dict) + floors (KOI) + prices (key→$/Mtok) (pure function).

    Args:
      run:    calibrate scored-run JSON (`{"candidates": [{label, overall_acc, ng_recall, is_reference}, ...]}`).
      floors: `{"kpi_floor": .., "ng_recall_floor": ..}` (floors from KOI.yaml).
      prices: `{candidate_key: in_mtok or None}` (from MODELS.yaml).
    Returns: structured result dict (passed/failed/cheapest_pass/max_failed_price/suggested_min_price).
    """
    kpi_floor = floors.get("kpi_floor") or 0.0
    ng_floor = floors.get("ng_recall_floor") or 0.0
    passed, failed = [], []
    for c in run.get("candidates", []):
        key = c.get("label")
        if c.get("is_reference") or key == "CASCADE":
            continue                       # reference (current = baseline) is not evaluated
        acc = c.get("overall_acc")
        ngr = c.get("ng_recall")           # may be None (e.g. no NG in gold)
        reasons = []
        if acc is not None and acc < kpi_floor:
            reasons.append(f"match rate {acc:.0%}<{kpi_floor:.0%}")
        if ngr is not None and ngr < ng_floor:
            reasons.append(f"NG recall {ngr:.0%}<{ng_floor:.0%}")
        rec = {"key": key, "price": prices.get(key),
               "overall_acc": acc, "ng_recall": ngr, "reasons": reasons}
        (failed if reasons else passed).append(rec)

    def _by_price(rs):
        return sorted(rs, key=lambda r: (r["price"] is None, r["price"] or 0.0))

    priced_pass = [r["price"] for r in passed if r["price"] is not None]
    priced_fail = [r["price"] for r in failed if r["price"] is not None]
    cheapest_pass = min(priced_pass) if priced_pass else None
    max_failed = max(priced_fail) if priced_fail else None
    # Propose a floor only when a failure record sits below cheapest_pass (= there is evidence of cheap-and-failing).
    failed_below = [p for p in priced_fail
                    if cheapest_pass is not None and p < cheapest_pass]
    suggested = cheapest_pass if failed_below else None
    return {
        "kpi_floor": kpi_floor, "ng_recall_floor": ng_floor,
        "passed": _by_price(passed), "failed": _by_price(failed),
        "cheapest_pass": cheapest_pass, "max_failed_price": max_failed,
        "suggested_min_price": suggested,
    }


def format_text(r):
    """Render the compute_floor result as human-facing text (material the orchestrator presents)."""
    lines = [
        f"floors check against past runs: passed {len(r['passed'])} / failed {len(r['failed'])} "
        f"(floor: match rate≥{r['kpi_floor']:.0%}, NG recall≥{r['ng_recall_floor']:.0%})"
    ]
    if r["failed"]:
        lines.append("  failed (price ascending):")
        for f in r["failed"]:
            pr = "?" if f["price"] is None else f"${f['price']:g}"
            lines.append(f"    {f['key']:22}{pr:>9}  {' / '.join(f['reasons'])}")
    if r["suggested_min_price"] is not None:
        lines.append(f"  → proposed --min-price {r['suggested_min_price']:g}"
                     f"(cheapest pass. below this = failure record / unproven ultra-cheap band, excluded from probe)")
        if r["max_failed_price"] is not None:
            lines.append(f"     for a looser search, a bit above the highest failure ${r['max_failed_price']:g} is also fine"
                         f"(the gap is unmeasured = a gamble)")
    else:
        lines.append("  → no failure-band floor proposed (no “cheap-and-failing” record, or all passed)")
    return "\n".join(lines)
