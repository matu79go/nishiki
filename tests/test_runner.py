"""Regression tests for the shared runner.py (generic runner shell, design doc §18.9, 2026-06-22).

With fake adapters (classification / non-classification):
  - resolve_candidates (ALL/NEW/KNOWN/CASCADE/comma)
  - run_candidate (per-item score, exception swallowing)
  - mode_run's run-JSON conforms to the schema read by koi_report/suggest_floor
    (classification = has ng_recall / non-classification = ng_recall None, floors = kpi only)
  - mode_probe cost projection

Run: cd nishiki && python3 -m tests.test_runner
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from nishiki import runner, scoring  # noqa: E402


# ── fake classification adapter (example_target equivalent, no network) ──
class FakeClassifier:
    catalog = {"cheap": {"model_id": "x.cheap", "in": 0.04, "out": 0.08},
               "good":  {"model_id": "x.good", "in": 0.5, "out": 2.0}}
    known = ["good"]
    labels = ["OK", "要確認", "NG"]
    positive = "NG"
    reference = "good"

    def __init__(self):
        self._model = None
        # gold: 4 items, 2 of them NG
        self._items = [{"id": "a", "gold": "OK"}, {"id": "b", "gold": "NG"},
                       {"id": "c", "gold": "要確認"}, {"id": "d", "gold": "NG"}]

    def load_items(self):
        return self._items

    def set_model(self, cand):
        self._model = cand

    def run_item(self, item):
        # cheap misses 1 NG (d→OK), good is perfect. Cost/latency depend on the model.
        gold = item["gold"]
        if self._model == "cheap":
            pred = "OK" if item["id"] == "d" else gold
            return pred, 0.001, 0.05, None
        return gold, 0.01, 0.2, None

    def score_item(self, pred, gold):
        return 1.0 if pred == gold else 0.0


# ── fake extraction adapter (CUAD equivalent, non-classification scalar) ──
class FakeExtractor:
    catalog = {"m1": {"model_id": "x.m1", "in": 0.1, "out": 0.2}}
    known = []
    labels = None          # non-classification = scalar KPI only
    positive = None
    reference = None

    def __init__(self):
        self._items = [{"id": "q1", "gold": [(0, 10)]}, {"id": "q2", "gold": [(5, 15)]}]

    def load_items(self):
        return self._items

    def set_model(self, cand):
        pass

    def run_item(self, item):
        # q1 is an exact match (F1=1.0), q2 is half off (F1=0.5)
        pred = [(0, 10)] if item["id"] == "q1" else [(0, 10)]
        return pred, 0.002, 0.1, None

    def score_item(self, pred, gold):
        return scoring.span_overlap_f1(pred, gold)


def test_resolve_candidates():
    cat = FakeClassifier.catalog
    assert runner.resolve_candidates("ALL", cat, ["good"]) == ["cheap", "good"]
    assert runner.resolve_candidates("NEW", cat, ["good"]) == ["cheap"]
    assert runner.resolve_candidates("KNOWN", cat, ["good"]) == ["good"]
    assert runner.resolve_candidates("good,cheap,good", cat, ["good"]) == ["good", "cheap"]
    assert runner.resolve_candidates("CASCADE,cheap", cat, ["good"]) == ["CASCADE", "cheap"]
    print("  ✓ resolve_candidates: ALL/NEW/KNOWN/CASCADE/comma, dedup")


def test_mode_run_classification():
    """classification adapter: extra dimensions like ng_recall + kpi=accuracy. good beats cheap."""
    out = runner.mode_run(FakeClassifier(), ["cheap", "good"], verbose=False)
    by = {c["label"]: c for c in out["candidates"]}
    # good=perfect → kpi 1.0 / ng_recall 1.0
    assert by["good"]["kpi"] == 1.0 and by["good"]["ng_recall"] == 1.0
    assert by["good"]["is_reference"] is True
    # cheap misses 1 NG (d) → accuracy 3/4, ng_recall 1/2
    assert abs(by["cheap"]["overall_acc"] - 0.75) < 1e-9
    assert abs(by["cheap"]["ng_recall"] - 0.5) < 1e-9 and by["cheap"]["ng_miss"] == ["d"]
    # the fields koi_report reads are all present
    for k in ("overall_acc", "ng_recall", "ng_total", "ng_miss_count", "cost_per_item",
              "latency_ms_p50", "latency_ms_p95"):
        assert k in by["cheap"], f"missing: {k}"
    # ordering is KPI descending → good first
    assert out["candidates"][0]["label"] == "good"
    print("  ✓ mode_run (classification): kpi=accuracy + ng_recall extra dimension + koi_report compatible")


def test_mode_run_extraction_nonclassification():
    """non-classification adapter: kpi=mean span F1 / ng_recall=None (floors=kpi only)."""
    out = runner.mode_run(FakeExtractor(), ["m1"], verbose=False)
    c = out["candidates"][0]
    # q1 F1=1.0, q2 F1=0.5 → kpi 0.75
    assert abs(c["kpi"] - 0.75) < 1e-9 and abs(c["overall_acc"] - 0.75) < 1e-9
    # non-classification = classification-only dimensions are None/0 (suggest_floor can safely ignore None)
    assert c["ng_recall"] is None and c["ng_total"] == 0 and c["ng_miss_count"] == 0
    # per_item carries the scores
    assert {p["id"]: p["score"] for p in c["per_item"]} == {"q1": 1.0, "q2": 0.5}
    print("  ✓ mode_run (non-classification): kpi=mean F1 / ng_recall=None / per_item scores")


def test_run_candidate_error_handling():
    """run_item raises → pred None, score 0, errs+1 (the run doesn't stop)."""
    class Boom(FakeExtractor):
        def run_item(self, item):
            if item["id"] == "q2":
                raise RuntimeError("boom")
            return [(0, 10)], 0.002, 0.1, None
    res, cost, errs = runner.run_candidate(Boom(), "m1", Boom()._items, verbose=False)
    assert errs == 1 and res["q2"]["pred"] is None and res["q2"]["score"] == 0.0
    assert res["q1"]["score"] == 1.0
    print("  ✓ run_candidate: swallow exception and continue (pred None/score 0/errs+1)")


def test_mode_probe():
    """probe: project the cost of probe_n items onto the full set."""
    out = runner.mode_probe(FakeClassifier(), ["cheap", "good"], probe_n=2, verbose=False)
    by = {c["label"]: c for c in out["candidates"]}
    # cheap: $0.002 over 2 items → projected $0.004 over 4 items
    assert abs(by["cheap"]["probe_cost"] - 0.002) < 1e-9
    assert abs(by["cheap"]["proj_full"] - 0.004) < 1e-9
    assert out["probe_n"] == 2 and out["n_inscope"] == 4
    print("  ✓ mode_probe: cost projection / cheapest first")


def main():
    fails = 0
    for fn in (test_resolve_candidates, test_mode_run_classification,
               test_mode_run_extraction_nonclassification, test_run_candidate_error_handling,
               test_mode_probe):
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
