"""Unit + integration test for the runtime probe (nishiki.autoprobe) and `nishiki run`.

Unit: default_adapter / patch_module idempotency. Integration: `nishiki run` injects the probe via
sitecustomize, wraps a dummy choke in a subprocess, and the call's usage lands in live.jsonl — the
agent's "source" (the dummy module) is never edited.
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from nishiki import autoprobe as AP  # noqa: E402


def test_default_adapter():
    # exact usage straight from a result dict (a typical call-wrapper return) — zero config, no estimation
    wrapper_ret = {"text": "...", "input_tokens": 5123, "output_tokens": 240,
                   "model_id": "qwen/qwen3-vl-32b-instruct"}
    ev = AP.default_adapter(("qwen/qwen3-vl-32b-instruct", "prompt"), {}, wrapper_ret)
    assert ev == {"model": "qwen/qwen3-vl-32b-instruct", "in_tokens": 5123, "out_tokens": 240}
    # nested usage (OpenAI/Bedrock-ish)
    ev2 = AP.default_adapter((), {}, {"model": "m", "usage": {"prompt_tokens": 10, "completion_tokens": 3}})
    assert ev2 == {"model": "m", "in_tokens": 10, "out_tokens": 3}
    # no usage in result → estimate from kwargs
    ev3 = AP.default_adapter((), {"model": "m", "prompt": "abcd efgh", "max_tokens": 77}, None)
    assert ev3["model"] == "m" and ev3["out_tokens"] == 77 and ev3["in_tokens"] >= 2
    # positional choke (model_id, prompt) with no usable result → estimate from args
    ev4 = AP.default_adapter(("mid", "some prompt text"), {"max_tokens": 9}, {"text": "x"})
    assert ev4["model"] == "mid" and ev4["out_tokens"] == 9
    assert AP.default_adapter((), {"prompt": "no model"}, None) is None
    print("  ✓ default_adapter: exact result usage / nested usage / kwargs+positional estimate fallback")


def test_patch_module_idempotent():
    import types
    m = types.ModuleType("dummy_probe_mod")
    m.calls = []
    m.converse = lambda **k: m.calls.append(k) or {"r": 1}
    log = os.path.join(tempfile.mkdtemp(), "live.jsonl")
    assert AP.patch_module(m, "converse", AP.default_adapter, log) is True
    assert AP.patch_module(m, "converse", AP.default_adapter, log) is False    # idempotent
    m.converse(model="x", prompt="hi", max_tokens=5)
    assert m.calls == [{"model": "x", "prompt": "hi", "max_tokens": 5}]         # original still runs
    ev = json.loads(open(log, encoding="utf-8").read().splitlines()[-1])
    assert ev["model"] == "x" and ev["out_tokens"] == 5
    print("  ✓ patch_module: wraps once / original runs / event logged")


def test_nishiki_run_wraps_choke():
    d = tempfile.mkdtemp(prefix="nz_run_")
    with open(os.path.join(d, "dummy_agent.py"), "w", encoding="utf-8") as f:
        f.write("def converse(model=None, prompt='', max_tokens=0):\n    return {'ok': True}\n")
    exp = tempfile.mkdtemp(prefix="nz_run_exp_")
    log = os.path.join(exp, "live.jsonl")
    env = dict(os.environ, PYTHONPATH=d + os.pathsep + os.environ.get("PYTHONPATH", ""))
    inner = "import dummy_agent; dummy_agent.converse(model='qwen-vl', prompt='verify', max_tokens=42)"
    r = subprocess.run(
        [sys.executable, "-m", "nishiki", "run", "--experiment", exp, "--choke", "dummy_agent:converse",
         "--", sys.executable, "-c", inner],
        env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"run failed: {r.stderr[:300]}"
    assert os.path.exists(log), f"no live.jsonl written. stdout={r.stdout[:300]} stderr={r.stderr[:300]}"
    ev = json.loads(open(log, encoding="utf-8").read().splitlines()[-1])
    assert ev["model"] == "qwen-vl" and ev["out_tokens"] == 42
    print("  ✓ nishiki run: probe wraps the choke in a subprocess → usage logged (no source edit)")


def main():
    fails = 0
    for fn in (test_default_adapter, test_patch_module_idempotent, test_nishiki_run_wraps_choke):
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
