"""Unit test for the edge KOI estimator (nishiki.koi_estimate) — no network, no model calls.

Verifies: text/image token heuristics, PNG dimension parsing, measured-cost reuse (reproduces the
measured KOI), from-prompt cost (tokens × price), and the vs-reference ratio (incl. an unpriced
composite reference reusing its measured cost).
"""
import json
import os
import struct
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from nishiki import koi_estimate as KE  # noqa: E402


def _profile():
    d = tempfile.mkdtemp(prefix="nz_est_")
    with open(os.path.join(d, "KOI.yaml"), "w", encoding="utf-8") as f:
        f.write("scorer:\n  kpi: agree\nreference: REF\n")
    with open(os.path.join(d, "MODELS.yaml"), "w", encoding="utf-8") as f:
        f.write("models:\n"
                "  a: { model_id: x/a, in: 1.0, out: 2.0 }\n"
                "  b: { model_id: x/b, in: 0.5, out: 1.0 }\n")     # REF has no price (composite route)
    os.makedirs(os.path.join(d, "runs"))
    run = {"mode": "run", "n": 10, "candidates": [
        {"label": "a", "agree": 0.9, "cost_per_item": 0.002},
        {"label": "b", "agree": 0.8, "cost_per_item": 0.001},
        {"label": "REF", "agree": 0.95, "cost_per_item": 0.010}]}
    with open(os.path.join(d, "runs", "100_run.json"), "w", encoding="utf-8") as f:
        json.dump(run, f)
    return d


def test_token_heuristics_and_png_dims():
    assert KE.text_tokens("") == 1 and KE.text_tokens("abcd") == 1 and KE.text_tokens("a" * 40) == 10
    d = tempfile.mkdtemp(prefix="nz_png_")
    p = os.path.join(d, "x.png")
    with open(p, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
                + struct.pack(">II", 800, 600) + b"\x08\x06\x00\x00\x00" + b"\x00" * 8)
    assert KE.image_dims(p) == (800, 600)
    assert KE.image_tokens(800, 600) == round(800 * 600 / 750)
    assert KE.image_dims(os.path.join(d, "nope.png")) is None
    print("  ✓ heuristics: text≈chars/4 / PNG dims parsed / image tokens area-based")


def test_measured_reuse_reproduces_koi():
    d = _profile()
    res = KE.estimate(d)
    by = {r["model"]: r for r in res["rows"]}
    assert res["kpi_name"] == "agree" and res["reference"] == "REF"
    assert abs(by["a"]["cost_per_item"] - 0.002) < 1e-12          # measured cost reused
    assert abs(by["a"]["koi"] - 0.9 / 0.002) < 1e-6              # KOI = kpi / cost
    assert by["a"]["cost_src"] == "measured cost reused"
    # vs-reference ratio = my KOI / REF's KOI (REF = 0.95/0.010 = 95)
    assert abs(by["a"]["vs_reference"] - (0.9 / 0.002) / (0.95 / 0.010)) < 1e-6
    print("  ✓ measured reuse: KOI reproduces kpi/cost / vs-reference ratio")


def test_from_prompt_cost_and_unpriced_reference():
    d = _profile()
    res = KE.estimate(d, in_tokens=1000, out_tokens=500)          # cost from tokens × price
    by = {r["model"]: r for r in res["rows"]}
    assert abs(by["a"]["cost_per_item"] - (1000 / 1e6 * 1.0 + 500 / 1e6 * 2.0)) < 1e-12
    assert by["a"]["cost_src"] == "from prompt"
    # REF is unpriced → reuse its measured cost so the ratio still resolves
    assert abs(by["REF"]["cost_per_item"] - 0.010) < 1e-12
    assert by["REF"]["cost_src"] == "measured (unpriced)"
    assert by["a"]["vs_reference"] is not None
    print("  ✓ from-prompt: cost = tokens × price / unpriced reference reuses measured cost")


def main():
    fails = 0
    for fn in (test_token_heuristics_and_png_dims, test_measured_reuse_reproduces_koi,
               test_from_prompt_cost_and_unpriced_reference):
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
