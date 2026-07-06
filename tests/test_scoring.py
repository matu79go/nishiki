"""Regression tests for the shared scoring.py (non-classification KPI generalization, design doc §18.9, 2026-06-22).

Key points:
  - verdict_extras produces the **same numbers** as the example_target glue score() (contract match = no classification regression).
  - aggregate_kpi is the per-item mean (score None counted as 0).
  - span_overlap_f1 is the overlap F1 of extracted spans (language-neutral).
  - latency_stats matches example_target's _latency_stats.

Run: cd nishiki && python3 -m tests.test_scoring
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from nishiki import scoring  # noqa: E402


# ── Ported **verbatim** from the example_target glue score() (contract reference implementation) ──
# Same logic as score() in adapters/example_target/calibrate.py. If verdict_extras produces the
# same numbers, classification semantics stay unchanged after generalization (no regression).
def _example_target_score_reference(gold_map, pred_map):
    VERDICTS = ["OK", "要確認", "NG"]
    n = len(gold_map)
    agree = sum(1 for vn, g in gold_map.items() if pred_map.get(vn) == g)
    g_ng = {vn for vn, g in gold_map.items() if g == "NG"}
    p_ng = {vn for vn, p in pred_map.items() if p == "NG"}
    tp = len(g_ng & p_ng)
    ng_recall = tp / len(g_ng) if g_ng else None
    ng_prec = tp / len(p_ng) if p_ng else None
    ng_miss = sorted(g_ng - p_ng)
    f1s = []
    for c in VERDICTS:
        g_c = {vn for vn, g in gold_map.items() if g == c}
        p_c = {vn for vn, p in pred_map.items() if p == c}
        t = len(g_c & p_c)
        rec = t / len(g_c) if g_c else None
        pre = t / len(p_c) if p_c else None
        f1 = (2 * pre * rec / (pre + rec)) if (pre and rec) else 0.0
        f1s.append(f1)
    macro_f1 = sum(f1s) / len(f1s)
    return {
        "n": n, "overall_agree": agree, "overall_acc": agree / n if n else 0.0,
        "ng_total": len(g_ng), "ng_recall": ng_recall, "ng_precision": ng_prec,
        "ng_miss": ng_miss, "ng_miss_count": len(ng_miss), "macro_f1": macro_f1,
    }


def test_verdict_extras_matches_example_target():
    """[regression] verdict_extras exactly matches example_target score() (no classification regression)."""
    gold = {"v1": "OK", "v2": "NG", "v3": "要確認", "v4": "NG", "v5": "OK", "v6": "NG"}
    pred = {"v1": "OK", "v2": "NG", "v3": "OK", "v4": "OK", "v5": "OK", "v6": "NG"}  # v3,v4 wrong
    ref = _example_target_score_reference(gold, pred)
    got = scoring.verdict_extras(gold, pred, labels=["OK", "要確認", "NG"], positive="NG")
    for k in ("overall_acc", "overall_agree", "ng_total", "ng_recall",
              "ng_precision", "ng_miss", "ng_miss_count", "macro_f1"):
        assert got[k] == ref[k], f"{k}: got {got[k]} != ref {ref[k]}"
    # 2 of 3 NG detected (v4 missed) = recall 2/3
    assert abs(got["ng_recall"] - 2 / 3) < 1e-9 and got["ng_miss"] == ["v4"]
    print(f"  ✓ verdict_extras == example_target score() (NG recall {got['ng_recall']:.2f}, full match)")


def test_verdict_extras_no_positive():
    """When gold has no positives (NG), ng_recall=None (safely absent even for classification)."""
    gold = {"a": "OK", "b": "要確認"}
    got = scoring.verdict_extras(gold, {"a": "OK", "b": "OK"}, ["OK", "要確認", "NG"], "NG")
    assert got["ng_recall"] is None and got["ng_total"] == 0
    print("  ✓ no positives → ng_recall=None")


def test_aggregate_kpi():
    """aggregate_kpi = mean score. score None counts as 0 (don't go easy on failures)."""
    items = [{"score": 1.0}, {"score": 0.5}, {"score": None}, {"score": 0.0}]
    assert abs(scoring.aggregate_kpi(items) - (1.5 / 4)) < 1e-9
    assert scoring.aggregate_kpi([]) == 0.0
    # For classification (score=1/0) this equals accuracy
    cls = [{"score": 1.0}, {"score": 1.0}, {"score": 0.0}]
    assert abs(scoring.aggregate_kpi(cls) - 2 / 3) < 1e-9
    print("  ✓ aggregate_kpi: mean / None=0 / classification = accuracy")


def test_span_overlap_f1():
    """span_overlap_f1: overlap F1 of character-offset sets (language-neutral)."""
    # exact match
    assert scoring.span_overlap_f1([(0, 10)], [(0, 10)]) == 1.0
    # both "no match" = 1.0 (correctly extracted nothing)
    assert scoring.span_overlap_f1([], []) == 1.0
    # only one side empty = 0.0
    assert scoring.span_overlap_f1([(0, 5)], []) == 0.0
    assert scoring.span_overlap_f1([], [(0, 5)]) == 0.0
    # half overlap: pred[0,10) gold[5,15) → inter 5, P=.5 R=.5 F1=.5
    assert abs(scoring.span_overlap_f1([(0, 10)], [(5, 15)]) - 0.5) < 1e-9
    # zero overlap = 0.0
    assert scoring.span_overlap_f1([(0, 5)], [(10, 15)]) == 0.0
    # multiple spans (same clause in 2 places): pred gets one exact + misses one → R drops
    f1 = scoring.span_overlap_f1([(0, 10)], [(0, 10), (20, 30)])
    assert abs(f1 - (2 * 1.0 * 0.5 / 1.5)) < 1e-9   # P=1.0 R=0.5
    print(f"  ✓ span_overlap_f1: exact/no-match/partial/multi-span (partial example {f1:.3f})")


def test_latency_stats():
    """latency_stats matches example_target's _latency_stats."""
    assert scoring.latency_stats([]) == (None, None, None)
    p50, p95, mean = scoring.latency_stats([100, 200, 300, 400])
    assert p50 == 300 and p95 == 400 and mean == 250
    print("  ✓ latency_stats: p50/p95/mean")


def test_scorer_registry():
    """[new] scoring registry: name→function, task_type→scorer name, unknown name errors (the mapping used by generic init)."""
    assert scoring.get_scorer("label_match")("a", "a") == 1.0
    assert scoring.get_scorer("label_match")("a", "b") == 0.0
    assert scoring.get_scorer("span_f1")([(0, 10)], [(0, 10)]) == 1.0
    assert scoring.get_scorer("sql_result_match") is scoring.sql_result_match
    assert scoring.TASK_SCORER["classification"] == "label_match"
    assert scoring.TASK_SCORER["extraction"] == "span_f1"
    assert scoring.TASK_SCORER["sql"] == "sql_result_match"
    try:
        scoring.get_scorer("nope")
        assert False, "no exception raised for unknown name"
    except ValueError as e:
        assert "nope" in str(e)
    print("  ✓ scorer registry: name resolution / task_type mapping / unknown-name error")


def test_sql_result_match():
    """Text-to-SQL execution accuracy = 1.0 iff the result set equals gold (order-insensitive, numeric-tolerant)."""
    m = scoring.sql_result_match
    # exact match (order within the set doesn't matter)
    assert m({"sql": "...", "rows": [[1, "a"], [2, "b"]]}, [[2, "b"], [1, "a"]]) == 1.0
    # integral-float tolerance: 100.0 == 100
    assert m({"sql": "...", "rows": [[100.0]]}, [[100]]) == 1.0
    # both empty (correctly "none") = match
    assert m({"sql": "...", "rows": []}, []) == 1.0
    # wrong rows = miss
    assert m({"sql": "...", "rows": [[3]]}, [[4]]) == 0.0
    # a query that errored (no rows key) = miss, never a crash
    assert m({"sql": "bad", "error": "no such table"}, [[1]]) == 0.0
    assert m(None, [[1]]) == 0.0
    # column order is significant (a genuinely different SELECT)
    assert m({"sql": "...", "rows": [["a", 1]]}, [[1, "a"]]) == 0.0
    print("  ✓ sql_result_match: set equality, numeric tolerance, empty, error/None, column order")


def main():
    fails = 0
    for fn in (test_verdict_extras_matches_example_target, test_verdict_extras_no_positive,
               test_aggregate_kpi, test_span_overlap_f1, test_latency_stats,
               test_scorer_registry, test_sql_result_match):
        try:
            print(f"[{fn.__name__}]")
            fn()
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"  ✗ FAIL: {type(e).__name__}: {e}")
    print("\n" + ("✅ all tests PASS" if not fails else f"❌ {fails} FAILED"))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
