"""Regression test for the CUAD adapter (non-classification KPI = extraction span F1, design doc §18.9, 2026-06-22).

Net/real-data independent (LLM calls inject a fake call, data is a synthetic fixture).
  - parse_cuad: SQuAD format → items (gold spans, is_impossible = empty)
  - locate_spans: extracted text → char spans (NONE/hallucination/multi-line)
  - extract_clause: choke calls call once → (text, in_tok, out_tok)
  - end-to-end: CuadAdapter × runner.mode_run → non-classification run-JSON (kpi = mean F1, ng_recall = None)
    → koi_report picks a best without wiping out (the shared path works for CUAD = proof of generalization)

Run: cd nishiki && python3 -m tests.test_cuad
"""
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))


def _load_cuad_module():
    """Load adapters/cuad/calibrate.py under a unique name (avoid clash with example_target/calibrate.py)."""
    path = os.path.join(ROOT, "adapters", "cuad", "calibrate.py")
    spec = importlib.util.spec_from_file_location("cuad_calibrate", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CU = _load_cuad_module()

# ── synthetic contract (CUAD-format fixture) ──────────────────────────────────────────────
_CONTEXT = ("This Agreement is entered into as of Jan 1, 2026. "
            "The term of this Agreement is five years. "
            "This Agreement shall be governed by the laws of New York.")
_TERM = "five years"
_LAW = "the laws of New York"


def _squad_fixture():
    return {"data": [{"title": "ACME", "paragraphs": [{"context": _CONTEXT, "qas": [
        {"id": "q-term", "question": "What is the term?",
         "answers": [{"text": _TERM, "answer_start": _CONTEXT.find(_TERM)}],
         "is_impossible": False},
        {"id": "q-law", "question": "What is the governing law?",
         "answers": [{"text": _LAW, "answer_start": _CONTEXT.find(_LAW)}],
         "is_impossible": False},
        {"id": "q-none", "question": "Is there an arbitration clause?",
         "answers": [], "is_impossible": True},
    ]}]}]}


def test_parse_cuad():
    items = CU.parse_cuad(_squad_fixture())
    assert len(items) == 3
    by = {it["id"].split("::")[1]: it for it in items}
    assert by["q-term"]["gold"] == [(_CONTEXT.find(_TERM), _CONTEXT.find(_TERM) + len(_TERM))]
    assert by["q-none"]["gold"] == [], "is_impossible = empty gold (no match)"
    assert by["q-law"]["question"] == "What is the governing law?"
    # limit takes effect
    assert len(CU.parse_cuad(_squad_fixture(), limit=2)) == 2
    print("  ✓ parse_cuad: gold span extraction / is_impossible = empty / limit")


def test_parse_cuad_balance():
    """balance=True: level impossible to the same count as answerable, interleaved (corrects the do-nothing trap)."""
    # synthetic: 2 answerable (q-term, q-law) + 3 impossible
    sq = {"data": [{"title": "T", "paragraphs": [{"context": _CONTEXT, "qas": [
        {"id": "a1", "question": "q1", "answers": [{"text": _TERM, "answer_start": _CONTEXT.find(_TERM)}], "is_impossible": False},
        {"id": "n1", "question": "q2", "answers": [], "is_impossible": True},
        {"id": "n2", "question": "q3", "answers": [], "is_impossible": True},
        {"id": "a2", "question": "q4", "answers": [{"text": _LAW, "answer_start": _CONTEXT.find(_LAW)}], "is_impossible": False},
        {"id": "n3", "question": "q5", "answers": [], "is_impossible": True},
    ]}]}]}
    items = CU.parse_cuad(sq, balance=True)
    n_ans = sum(1 for it in items if it["gold"])
    n_imp = sum(1 for it in items if not it["gold"])
    assert n_ans == 2 and n_imp == 2, f"not balanced: ans={n_ans} imp={n_imp}"
    # interleaved (first is answerable, second is impossible) = probe is not skewed
    assert items[0]["gold"] and not items[1]["gold"]
    # balance=False keeps all (2 with match + 3 without = 5)
    assert len(CU.parse_cuad(sq, balance=False)) == 5
    print("  ✓ parse_cuad(balance): leveled to same count, interleaved / off = all")


def test_locate_spans():
    ctx = _CONTEXT
    # exact verbatim → correct position
    assert CU.locate_spans(ctx, _TERM) == [(ctx.find(_TERM), ctx.find(_TERM) + len(_TERM))]
    # NONE / empty → []
    assert CU.locate_spans(ctx, "NONE") == [] and CU.locate_spans(ctx, "") == []
    # hallucination (not in the contract) → drop it (penalized in scoring)
    assert CU.locate_spans(ctx, "ten years and a pony") == []
    # multi-line + stripping of quotes / bullet markers
    multi = f'- "{_TERM}"\n- {_LAW}'
    spans = CU.locate_spans(ctx, multi)
    assert (ctx.find(_TERM), ctx.find(_TERM) + len(_TERM)) in spans
    assert (ctx.find(_LAW), ctx.find(_LAW) + len(_LAW)) in spans
    print("  ✓ locate_spans: verbatim position / NONE / hallucination dropped / multi-line")


def test_extract_clause_choke():
    """choke=extract_clause calls call once and returns (text, in, out)."""
    calls = []

    def fake(model_id, prompt, max_tokens=1024):
        calls.append((model_id, prompt))
        assert "five years" in prompt  # context goes into the prompt
        return {"text": _TERM, "input_tokens": 100, "output_tokens": 5}

    text, itok, otok = CU.extract_clause(_CONTEXT, "What is the term?", "m1", call=fake)
    assert text == _TERM and itok == 100 and otok == 5 and len(calls) == 1
    print("  ✓ extract_clause: call once / returns (text, in, out)")


# ── fake LLM: good = returns correct verbatim / cheap-bad = returns NONE ─────────────────
_QA = {"What is the term?": _TERM, "What is the governing law?": _LAW,
       "Is there an arbitration clause?": "NONE"}


def _fake_call_factory(model_id_good):
    def fake(model_id, prompt, max_tokens=1024):
        q = prompt.split("# Question\n", 1)[1].split("\n", 1)[0]
        ans = _QA.get(q, "NONE")
        if model_id != model_id_good:
            ans = "NONE"                      # the bad model extracts nothing
        return {"text": ans, "input_tokens": 200, "output_tokens": 10}
    return fake


def test_end_to_end_run_and_report():
    """CuadAdapter × runner.mode_run → non-classification run-JSON → koi_report passes (no wipeout)."""
    items = CU.parse_cuad(_squad_fixture())
    catalog = {"good": {"model_id": "x/good", "in": 0.5, "out": 2.0},
               "bad":  {"model_id": "x/bad", "in": 0.05, "out": 0.4}}
    adapter = CU.CuadAdapter(items=items, catalog=catalog, known=["good"],
                             call=_fake_call_factory("x/good"))
    out = CU.runner.mode_run(adapter, ["good", "bad"], verbose=False)
    by = {c["label"]: c for c in out["candidates"]}
    # good: extracts q-term/q-law verbatim (F1=1.0), q-none also correctly no-match (F1=1.0) → kpi 1.0
    assert abs(by["good"]["kpi"] - 1.0) < 1e-9
    # bad: all NONE → q-term/q-law empty vs gold (F1=0), only q-none correct (F1=1.0) → kpi 1/3
    assert abs(by["bad"]["kpi"] - 1 / 3) < 1e-9
    # non-classification = ng_recall is None (no classification-only dimension)
    assert by["good"]["ng_recall"] is None and by["bad"]["ng_recall"] is None
    # ordering is KPI descending → good first
    assert out["candidates"][0]["label"] == "good"

    # run it through koi_report: at kpi_floor=0.8 good survives / bad drops, no wipeout even without NG
    from nishiki import koi_report
    d = tempfile.mkdtemp(prefix="nishiki_cuad_")
    rj, ky, html = (os.path.join(d, "r.json"), os.path.join(d, "KOI.yaml"), os.path.join(d, "o.html"))
    with open(rj, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    with open(ky, "w", encoding="utf-8") as f:
        f.write("floors: {kpi_floor: 0.8}\n")
    _path, best = koi_report.generate(rj, ky, html)
    assert best == "good", f"best={best} (CUAD non-classification is selectable in koi_report)"
    print("  ✓ E2E: CuadAdapter→mode_run (kpi=mean F1/ng=None)→koi_report picks best")


def main():
    fails = 0
    for fn in (test_parse_cuad, test_parse_cuad_balance, test_locate_spans,
               test_extract_clause_choke, test_end_to_end_run_and_report):
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
