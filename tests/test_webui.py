"""Tests for the live KOI web dashboard helpers (webui) — pure data, no server/sockets.

Builds a tiny real profile in a temp dir (KOI.yaml / MODELS.yaml / runs/*_run.json + live.jsonl) and
checks: read_events parses calls, call_row produces a per-candidate breakdown for the same input, and
snapshot returns the leaderboard + replayed calls. Also pins the KPI guard: a run that recorded a
count-named KPI (overall_agree) must still yield a RATE in [0,1] (the 2200% bug must stay fixed).

Run: cd nishiki && python3 -m tests.test_webui
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, SRC)

from nishiki import koi_estimate, koi_report, webui  # noqa: E402


def _profile(d):
    """Write a minimal profile whose run records a count-named KPI (overall_agree) to exercise the guard."""
    open(os.path.join(d, "KOI.yaml"), "w", encoding="utf-8").write(
        "target: t\nreference: B\nscorer:\n  kpi: overall_agree\nfloors:\n  kpi_floor: 0.8\n")
    open(os.path.join(d, "MODELS.yaml"), "w", encoding="utf-8").write(
        "models:\n  A:\n    in: 0.1\n    out: 0.4\n    model_id: A\n"
        "  B:\n    in: 0.2\n    out: 0.2\n    model_id: B\n")
    os.makedirs(os.path.join(d, "runs"), exist_ok=True)
    # per_item: generically-named list of {gold,pred,cost} so measured_points can detect it by shape
    a_items = [{"id": 1, "gold": "ok", "pred": "ok", "cost": 0.0002, "latency_ms": 1000},
               {"id": 2, "gold": "ok", "pred": "ng", "cost": 0.0002, "latency_ms": 1200}]
    b_items = [{"id": 1, "gold": "ok", "pred": "ok", "cost": 0.0006, "latency_ms": 1600},
               {"id": 2, "gold": "ok", "pred": "ok", "cost": 0.0006, "latency_ms": 1800}]
    run = {"n": 27, "candidates": [
        {"label": "A", "overall_agree": 22, "overall_acc": 22 / 27, "kpi": 22 / 27,
         "cost_per_item": 0.0002, "latency_ms_p50": 1100, "latency_ms_p95": 1900,
         "is_reference": False, "per_item": a_items},
        {"label": "B", "overall_agree": 25, "overall_acc": 25 / 27, "kpi": 25 / 27,
         "cost_per_item": 0.0006, "latency_ms_p50": 1700, "latency_ms_p95": 2800,
         "is_reference": True, "per_item": b_items},
    ]}
    json.dump(run, open(os.path.join(d, "runs", "1_run.json"), "w", encoding="utf-8"))
    log = os.path.join(d, "live.jsonl")
    with open(log, "w", encoding="utf-8") as f:
        f.write(json.dumps({"model": "A", "in_tokens": 1000, "out_tokens": 250, "latency_ms": 1234}) + "\n")
        f.write("not json\n")                                       # garbage line must be skipped
        f.write(json.dumps({"model": "B", "in_tokens": 800, "out_tokens": 200}) + "\n")
    return log


def test_read_events_skips_garbage():
    with tempfile.TemporaryDirectory() as d:
        log = _profile(d)
        evs = webui.read_events(log)
        assert len(evs) == 2, f"expected 2 events (garbage skipped), got {len(evs)}"
        assert evs[0]["model"] == "A" and evs[1]["model"] == "B"
    print("  ✓ read_events: parses model calls, skips non-JSON lines")


def test_kpi_guard_count_to_rate():
    """A run that named a COUNT field as the KPI must still load as a RATE in [0,1] (2200% bug stays fixed)."""
    with tempfile.TemporaryDirectory() as d:
        _profile(d)
        basis = koi_estimate.load_basis(d)
        for m, meas in basis["measured"].items():
            assert 0.0 <= meas["kpi"] <= 1.0, f"{m}: KPI {meas['kpi']} not a rate in [0,1]"
        assert abs(basis["measured"]["A"]["kpi"] - 22 / 27) < 1e-9   # fell back to overall_acc
    print("  ✓ KPI guard: count-named scorer falls back to the rate (KPI ≤ 1)")


def test_call_row_per_candidate():
    with tempfile.TemporaryDirectory() as d:
        log = _profile(d)
        basis = koi_estimate.load_basis(d)
        ev = webui.read_events(log)[0]                              # the call routed to A
        row = webui.call_row(d, ev, basis, 0)
        assert row["model"] == "A" and row["i"] == 0
        assert set(row["per"]) == {"A", "B"}, "per must estimate every candidate for the same input"
        assert row["latency_ms"] == 1234                            # actual-route latency passed through
        assert row["cost"] is not None and row["koi"] is not None
        # cheaper model B at similar KPI → both have a positive KOI; A is the routed one
        assert row["per"]["A"]["koi"] > 0 and row["per"]["B"]["koi"] > 0
    print("  ✓ call_row: per-candidate cost/KOI for one input + actual-route latency")


def test_snapshot_shape():
    with tempfile.TemporaryDirectory() as d:
        log = _profile(d)
        basis = koi_estimate.load_basis(d)
        snap = webui.snapshot(d, log, basis)
        assert snap["reference"] == "B"
        assert len(snap["rows"]) == 2 and len(snap["calls"]) == 2
        assert all("latency_ms_p50" in r for r in snap["rows"]), "rows carry measured latency"
        assert all(0.0 <= (r["kpi"] or 0) <= 1.0 for r in snap["rows"]), "leaderboard KPI is a rate"
        assert "measured" in snap and len(snap["measured"]) == 2, "snapshot carries per-item measured points"
    print("  ✓ snapshot: leaderboard rows (with latency) + replayed calls + measured points")


def test_per_item_list_detected_by_shape():
    """The per-item list is found by shape (list of dicts with 'cost'), not by a hardcoded key name."""
    cand = {"label": "A", "ng_miss": [], "blob": [1, 2, 3],
            "per_anything": [{"gold": "x", "pred": "x", "cost": 0.1}]}
    got = webui._per_item_list(cand)
    assert got and got[0]["cost"] == 0.1, "must detect the cost-bearing list regardless of its key"
    assert webui._per_item_list({"label": "A", "ng_miss": []}) is None, "no cost-bearing list → None"
    print("  ✓ _per_item_list: shape-based detection (target-agnostic, skips non-cost lists)")


def test_koi_report_fa_rate_generic_key():
    """koi_report._fa_rate finds the per-item list BY SHAPE — works whatever the per-item key is named."""
    c = {"label": "A", "per_records": [
        {"gold": "NG", "pred": "NG", "cost": 0.1},     # true positive
        {"gold": "ok", "pred": "NG", "cost": 0.1},     # false alarm (gold != NG but predicted NG)
        {"gold": "ok", "pred": "ok", "cost": 0.1}]}    # correct negative
    fa = koi_report._fa_rate(c)                          # 1 false alarm out of 2 non-NG items → 0.5
    assert fa is not None and abs(fa - 0.5) < 1e-9, f"expected 0.5, got {fa}"
    assert koi_report._fa_rate({"label": "A"}) is None, "no per-item list → None (not a crash)"
    print("  ✓ koi_report._fa_rate: per-item list found by shape (any key name, no hardcode)")


def test_measured_points_running_koi():
    with tempfile.TemporaryDirectory() as d:
        _profile(d)
        basis = koi_estimate.load_basis(d)
        pts = webui.measured_points(basis)
        assert len(pts) == 2, "one point per measured item"
        assert pts[0]["model"] == "B", "points anchor on the reference"
        # B is correct on both items → running accuracy 1.0; KOI = 1.0 / 0.0006
        assert abs(pts[0]["per"]["B"]["koi"] - 1.0 / 0.0006) < 1.0
        # A is right then wrong → running accuracy 1.0 then 0.5 (running KOI drops)
        assert pts[1]["per"]["A"]["koi"] < pts[0]["per"]["A"]["koi"]
        # cumulative cost grows
        assert pts[1]["per"]["B"]["cost"] == 0.0006
    print("  ✓ measured_points: per-item points with running KOI (per candidate, aligned by index)")


def main():
    fails = 0
    for fn in (test_read_events_skips_garbage, test_kpi_guard_count_to_rate,
               test_call_row_per_candidate, test_snapshot_shape,
               test_per_item_list_detected_by_shape, test_koi_report_fa_rate_generic_key,
               test_measured_points_running_koi):
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
