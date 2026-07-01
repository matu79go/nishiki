"""Unit test for the live KOI HUD helpers (nishiki.live) — pure functions, no loop / no model calls."""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from nishiki import live as L          # noqa: E402
from nishiki import koi_estimate as KE  # noqa: E402


def _profile():
    d = tempfile.mkdtemp(prefix="nz_live_")
    with open(os.path.join(d, "KOI.yaml"), "w", encoding="utf-8") as f:
        f.write("scorer:\n  kpi: agree\nreference: REF\n")
    with open(os.path.join(d, "MODELS.yaml"), "w", encoding="utf-8") as f:
        f.write("models:\n  a: { model_id: x/a, in: 1.0, out: 2.0 }\n  b: { model_id: x/b, in: 0.5, out: 1.0 }\n")
    os.makedirs(os.path.join(d, "runs"))
    run = {"candidates": [
        {"label": "a", "agree": 0.9, "cost_per_item": 0.002},
        {"label": "b", "agree": 0.8, "cost_per_item": 0.001},
        {"label": "REF", "agree": 0.95, "cost_per_item": 0.010}]}
    with open(os.path.join(d, "runs", "1_run.json"), "w", encoding="utf-8") as f:
        json.dump(run, f)
    return d


def test_parse_event():
    assert L.parse_event("") is None and L.parse_event("not json") is None
    assert L.parse_event("[1,2]") is None                        # not a dict
    ev = L.parse_event(json.dumps({"model": "a", "prompt": "hi", "out_tokens": 64, "junk": 1}))
    assert ev == {"model": "a", "prompt": "hi", "out_tokens": 64}  # only known fields kept
    d = tempfile.mkdtemp()
    pf = os.path.join(d, "p.txt")
    open(pf, "w", encoding="utf-8").write("from file")
    ev2 = L.parse_event(json.dumps({"model": "a", "prompt_file": pf}))
    assert ev2["prompt"] == "from file"                          # prompt_file is read in
    print("  ✓ parse_event: json→event / known fields / prompt_file resolved / junk rejected")


def test_rolling_stats():
    assert L.rolling_stats([])["avg_koi"] is None
    s = L.rolling_stats([(0.002, 450.0), (0.001, 800.0), (0.002, 400.0)])
    assert s["n"] == 3 and abs(s["avg_koi"] - 550.0) < 1e-9
    assert abs(s["avg_cost"] - (0.005 / 3)) < 1e-9 and s["drift_pct"] is not None
    print("  ✓ rolling_stats: avg cost/KOI + drift over the window")


def test_render_frame():
    d = _profile()
    res = KE.estimate(d, in_tokens=1000, out_tokens=500)
    stats = L.rolling_stats([(0.002, 450.0), (0.002, 450.0)])
    frame = L.render_frame(res, "a", stats, now_str="12:00:00")
    assert "Nishiki live KOI" in frame and "current route: a" in frame
    assert "a" in frame and "b" in frame and "REF (ref)" in frame   # reference marked
    assert "▸ a" in frame                                          # current route marked
    assert "updated 12:00:00" in frame and "rolling" in frame
    print("  ✓ render_frame: framed view / route marked / reference labelled / rolling + clock")


def main():
    fails = 0
    for fn in (test_parse_event, test_rolling_stats, test_render_frame):
        try:
            print(f"[{fn.__name__}]")
            fn()
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"  ✗ FAIL: {type(e).__name__}: {e}")
    print("\n" + ("✅ all tests PASS" if not fails else f"❌ {fails} FAIL"))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
