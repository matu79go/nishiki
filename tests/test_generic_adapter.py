"""Regression tests for the generic adapter (the core of the release, design doc §18.9, 2026-06-22).

Demonstrates that "you can measure with config (KOI.yaml) alone, no hand-written code per target"
on both CUAD (non-classification = extraction) and a synthetic classification task. No network
(a fake backend is injected).

Run: cd nishiki && python3 -m tests.test_generic_adapter
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from nishiki import generic_adapter as GA, runner, koi_report  # noqa: E402

_CTX = ("This Agreement is dated Jan 1, 2026. The term is five years. "
        "It is governed by the laws of New York.")
_TERM, _LAW = "five years", "the laws of New York"


def _squad_file():
    sq = {"data": [{"title": "ACME", "paragraphs": [{"context": _CTX, "qas": [
        {"id": "term", "question": "What is the term?",
         "answers": [{"text": _TERM, "answer_start": _CTX.find(_TERM)}], "is_impossible": False},
        {"id": "law", "question": "Governing law?",
         "answers": [{"text": _LAW, "answer_start": _CTX.find(_LAW)}], "is_impossible": False},
        {"id": "none", "question": "Arbitration?", "answers": [], "is_impossible": True},
    ]}]}]}
    d = tempfile.mkdtemp(prefix="ga_")
    p = os.path.join(d, "cuad.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(sq, f)
    return p


def test_load_squad():
    items = GA.load_squad(_squad_file())
    by = {it["id"].split("::")[1]: it for it in items}
    assert by["term"]["gold"] == [(_CTX.find(_TERM), _CTX.find(_TERM) + len(_TERM))]
    assert by["none"]["gold"] == []
    print("  ✓ load_squad: gold spans / is_impossible=empty")


def test_parsers():
    item = {"context": _CTX}
    assert GA.parse_locate_spans(item, _TERM) == [(_CTX.find(_TERM), _CTX.find(_TERM) + len(_TERM))]
    assert GA.parse_locate_spans(item, "NONE") == []
    assert GA.parse_identity({}, "  hi  ") == "hi"
    assert GA.parse_label({}, "NG だと思う", ["OK", "NG"]) == "NG"
    print("  ✓ parsers: locate_spans / identity / label")


def test_locate_spans_normalized():
    """[new] match by absorbing whitespace/newline and case variation (contracts are full of newlines → rescue exact-match failures)."""
    ctx = "The term of\nthis  Agreement is FIVE YEARS from the Effective Date."
    # model returns newlines as spaces and collapses runs of spaces → exact wouldn't find it, but normalization does
    sp = GA._find_span(ctx, "this Agreement is FIVE YEARS")
    assert sp is not None and ctx[sp[0]:sp[1]].replace("\n", " ").replace("  ", " ") \
        .startswith("this Agreement is FIVE YEARS"[:10])
    # matches case-insensitively (keep the verbatim essence, absorb only case)
    assert GA._find_span(ctx, "five years") is not None
    # paraphrases (words not in the contract) don't match = the verbatim-extraction penalty is preserved
    assert GA._find_span(ctx, "a duration of sixty months") is None
    print("  ✓ locate_spans normalization: absorb whitespace/newline/case, keep paraphrase non-matching")


# fake backend: good=correct verbatim / bad=NONE
_QA = {"What is the term?": _TERM, "Governing law?": _LAW, "Arbitration?": "NONE"}


def _fake(good_id):
    def call(model_id, prompt, max_tokens=1024):
        q = prompt.split("Q: ", 1)[1].split("\n", 1)[0]
        ans = _QA.get(q, "NONE") if model_id == good_id else "NONE"
        return {"text": ans, "input_tokens": 200, "output_tokens": 10}
    return call


def test_generic_cuad_end_to_end():
    """[core] No hand-written adapter = GenericAdapter (config only) measures CUAD non-classification all the way through koi_report."""
    config = {
        "task_type": "extraction", "kpi": "span_f1",
        "gold_format": "squad", "gold_data": _squad_file(),
        "prompt": "Extract the verbatim span answering the question, or NONE.\nQ: {question}\n\n{context}",
        "parser": "locate_spans", "balance": False,
    }
    catalog = {"good": {"model_id": "x/good", "in": 0.5, "out": 2.0},
               "bad": {"model_id": "x/bad", "in": 0.05, "out": 0.4}}
    adapter = GA.GenericAdapter(config, catalog, call=_fake("x/good"))
    out = runner.mode_run(adapter, ["good", "bad"], verbose=False)
    by = {c["label"]: c for c in out["candidates"]}
    assert abs(by["good"]["kpi"] - 1.0) < 1e-9          # good=all correct → F1 1.0
    assert abs(by["bad"]["kpi"] - 1 / 3) < 1e-9         # bad=all NONE → only none is correct
    assert by["good"]["ng_recall"] is None              # non-classification = no classification dimensions
    assert out["candidates"][0]["label"] == "good"
    # run it through koi_report (kpi_floor only)
    d = tempfile.mkdtemp(prefix="ga_koi_")
    rj, ky, html = (os.path.join(d, "r.json"), os.path.join(d, "KOI.yaml"), os.path.join(d, "o.html"))
    with open(rj, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    with open(ky, "w", encoding="utf-8") as f:
        f.write("floors: {kpi_floor: 0.8}\n")
    _p, best = koi_report.generate(rj, ky, html)
    assert best == "good"
    print("  ✓ [core] GenericAdapter: config only, CUAD non-classification → KOI report (no hand-written adapter)")


def test_generic_classification():
    """Classification tasks are also config-only: label parser + label_match + labels also yields ng_recall."""
    items = [{"id": "1", "text": "請求書OK", "gold": "OK"},
             {"id": "2", "text": "異常あり", "gold": "NG"},
             {"id": "3", "text": "微妙", "gold": "NG"}]
    config = {"task_type": "classification", "kpi": "label_match",
              "prompt": "分類: {text}", "parser": "label",
              "labels": ["OK", "NG"], "positive": "NG"}
    catalog = {"m": {"model_id": "x/m", "in": 0.1, "out": 0.2}}

    def call(model_id, prompt, max_tokens=1024):
        # NG if text contains "異常", otherwise OK (id3 slips through = 1 NG miss)
        ng = "異常" in prompt
        return {"text": "NG" if ng else "OK", "input_tokens": 50, "output_tokens": 2}
    adapter = GA.GenericAdapter(config, catalog, items=items, call=call)
    out = runner.mode_run(adapter, ["m"], verbose=False)
    c = out["candidates"][0]
    assert abs(c["overall_acc"] - 2 / 3) < 1e-9         # id3 wrong
    assert abs(c["ng_recall"] - 0.5) < 1e-9             # 1 of 2 NG detected
    print("  ✓ classification is also config-only: label_match + ng_recall (the generic adapter handles both)")


def test_from_koi_closes_loop():
    """[core] build_koi_yaml (auto-generate) → KOI.yaml → from_koi → run. The generated config runs directly."""
    import yaml
    from nishiki import init_cmd
    koi_text = init_cmd.build_koi_yaml(
        "cuad", "cuad_agent:extract", task_type="extraction",
        gold_data=_squad_file(), candidates=["good", "bad"],
        run={"gold_format": "squad", "parser": "locate_spans",
             "prompt": "Extract the span or NONE.\nQ: {question}\n\n{context}"})
    koi = yaml.safe_load(koi_text)
    # the generated KOI.yaml has a run block and span_f1
    assert koi["run"]["parser"] == "locate_spans" and koi["scorer"]["kpi"] == "span_f1"
    catalog = {"good": {"model_id": "x/good", "in": 0.5, "out": 2.0},
               "bad": {"model_id": "x/bad", "in": 0.05, "out": 0.4}}
    adapter = GA.GenericAdapter.from_koi(koi, catalog, call=_fake("x/good"))
    out = runner.mode_run(adapter, ["good", "bad"], verbose=False)
    by = {c["label"]: c for c in out["candidates"]}
    assert abs(by["good"]["kpi"] - 1.0) < 1e-9 and abs(by["bad"]["kpi"] - 1 / 3) < 1e-9
    print("  ✓ [core] build_koi_yaml→from_koi→run (auto-generated KOI.yaml runs directly = zero hand-writing)")


def main():
    fails = 0
    for fn in (test_load_squad, test_parsers, test_locate_spans_normalized,
               test_generic_cuad_end_to_end,
               test_generic_classification, test_from_koi_closes_loop):
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
