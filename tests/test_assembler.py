"""⑤ Regression test: the deterministic part of the auto-assembler (self-contained, no external gold dependency).

Design (2026-06-20, plan (b)): MODELS.yaml = **the raw full enumeration** (not compressed = a menu).
Choosing the optimal subset is AI recommendation + human confirmation (non-deterministic = out of test scope).
So what we verify here is:
  - parse_src_models: src fact extraction from the target code (if example_target exists)
  - curate: full enumeration / src-first dedup / keep missing prices as None (no fabrication) / fact normalization
  - to_models_yaml: serialize→parse round-trip soundness
  - build_koi_yaml: structure + candidates (ALL / recommended list) rendering

Run: cd nishiki && python3 -m tests.test_assembler
"""
import json
import os
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)               # project dir (nishiki)
sys.path.insert(0, os.path.join(ROOT, "src"))   # src-layout: package is src/nishiki

from nishiki import init_cmd, models_cmd  # noqa: E402

EXAMPLE_SRC = "/path/to/your/agent"

# ── hermetic fake target (run the assembler without example_target and without the net) ────────
_SRC_CONSTANTS = '''\
LLM_MODELS = {
    "qwen-vl": "qwen.qwen3-vl-235b-a22b",
    "claude-sonnet": "us.anthropic.claude-sonnet-4-6",
}
LLM_PRICING_USD_PER_MTOK = {
    "qwen-vl": (0.53, 2.66),         # (B)
    "claude-sonnet": (3.00, 15.00),  # (A)
}
LLM_CASCADE = ["qwen-vl", "claude-sonnet"]
'''

# minimal fixture matching the fetch_catalog() (OpenRouter) output format (in_per_1k is per-1k).
_OPENROUTER_FIXTURE = [
    {"id": "qwen/qwen3-vl", "name": "qwen-or", "vision": True,
     "in_per_1k": 0.0005, "out_per_1k": 0.002},
    {"id": "openai/gpt-5-nano", "name": "gpt", "vision": True,
     "in_per_1k": 0.00005, "out_per_1k": 0.0004},
    {"id": "meta/llama-4-scout", "name": "llama", "vision": True,
     "in_per_1k": 0.0001, "out_per_1k": 0.0003},
    {"id": "router/free", "name": "free", "vision": True,          # price 0 = excluded
     "in_per_1k": 0.0, "out_per_1k": 0.0},
]


def _fake_target():
    d = tempfile.mkdtemp(prefix="nishiki_src_")
    with open(os.path.join(d, "constants.py"), "w", encoding="utf-8") as f:
        f.write(_SRC_CONSTANTS)
    return d


class _patch_fetch_catalog:
    """Swap models_cmd.fetch_catalog for the fixture (no net). Restores on with-exit."""
    def __init__(self, fixture):
        self.fixture = fixture
    def __enter__(self):
        self.orig = models_cmd.fetch_catalog
        models_cmd.fetch_catalog = lambda *a, **k: list(self.fixture)
    def __exit__(self, *exc):
        models_cmd.fetch_catalog = self.orig
        return False

# minimal fixture mimicking the fetch_bedrock_catalog() output format (includes unlisted-price = LEGACY-equivalent)
CATALOG_FIXTURE = [
    {"id": "qwen.qwen3-vl-235b-a22b", "name": "qwen", "vision": True, "call": "on_demand",
     "in_per_1k": 0.00053, "out_per_1k": 0.00266, "lifecycle": "ACTIVE", "origin": "bedrock"},
    {"id": "google.gemma-3-4b-it", "name": "gemma", "vision": True, "call": "on_demand",
     "in_per_1k": 0.00004, "out_per_1k": 0.00008, "lifecycle": "ACTIVE", "origin": "bedrock"},
    {"id": "moonshotai.kimi-k2.5", "name": "kimi", "vision": True, "call": "on_demand",
     "in_per_1k": 0.0006, "out_per_1k": 0.003, "lifecycle": "ACTIVE", "origin": "bedrock"},
    {"id": "anthropic.claude-3-haiku-20240307-v1:0", "name": "legacy", "vision": True,
     "call": "on_demand", "in_per_1k": None, "out_per_1k": None,
     "lifecycle": "LEGACY", "origin": "bedrock"},   # price unlisted → kept as needs-price
]


def test_parse_src_facts():
    """parse_src_models correctly extracts the current models from example_target."""
    if not os.path.exists(EXAMPLE_SRC):
        print("  ~ skip (no example_target source)")
        return
    src, cascade = models_cmd.parse_src_models(EXAMPLE_SRC)
    by = {m["key"]: m for m in src}
    assert cascade == ["qwen-vl", "claude-sonnet"], f"cascade: {cascade}"
    assert by["qwen-vl"]["model_id"] == "qwen.qwen3-vl-235b-a22b"
    assert by["qwen-vl"]["call"] == "on_demand" and by["qwen-vl"]["tier"] == "jp"
    assert by["nova-pro"]["tier"] == "apac" and by["nova-pro"]["call"] == "profile"
    assert by["claude-sonnet"]["in_mtok"] == 3.0 and by["claude-sonnet"]["price_src"] == "A"
    assert by["qwen-vl"]["price_src"] == "B"
    assert all(m["origin"] == "src" for m in src)
    print(f"  ✓ src {len(src)} kinds: model_id/call/tier/price/source all extracted correctly")


def test_curate_full_list():
    """curate keeps the full enumeration (no compression), src-first dedup, missing prices as None."""
    src = [
        {"key": "qwen-vl", "origin": "src", "model_id": "qwen.qwen3-vl-235b-a22b",
         "call": "on_demand", "in_mtok": 0.53, "out_mtok": 2.66, "tier": "jp",
         "price_src": "B", "quality_hint": "unknown", "note": ""},
    ]
    models = models_cmd.curate(src, CATALOG_FIXTURE)
    by_id = {m["model_id"]: m for m in models}

    # src qwen suppresses the same bedrock id → one entry (src-first dedup)
    assert len(models) == 4, f"count: {len(models)} (src1 + bedrock3, qwen duplicate removed)"
    assert by_id["qwen.qwen3-vl-235b-a22b"]["origin"] == "src"

    # LEGACY/unlisted-price not dropped, kept as None + note=needs-price (no compression, no fabrication)
    legacy = by_id["anthropic.claude-3-haiku-20240307-v1:0"]
    assert legacy["in_mtok"] is None and "needs price" in legacy["note"]

    # bedrock tier is ap-ne1, per-1k → per-Mtok conversion is correct
    gemma = by_id["google.gemma-3-4b-it"]
    assert gemma["tier"] == "ap-ne1" and gemma["in_mtok"] == 0.04 and gemma["price_src"] == "B"

    # serialize→parse round-trip (YAML soundness)
    parsed = yaml.safe_load(models_cmd.to_models_yaml(models))["models"]
    assert len(parsed) == 4
    print(f"  ✓ curate: all {len(models)} enumerated / src-first dedup / missing price = needs-price / round-trip YAML sound")


def test_curate_openrouter_origin():
    """curate(origin=openrouter): tier=external / openai_compatible / price_src=OR."""
    cat = [{"id": "openai/gpt-5-nano", "name": "gpt", "vision": True,
            "in_per_1k": 0.00005, "out_per_1k": 0.0004, "origin": "openrouter"}]
    models = models_cmd.curate([], cat, origin="openrouter")
    m = models[0]
    assert m["origin"] == "openrouter" and m["tier"] == "external"
    assert m["call"] == "openai_compatible" and m["price_src"] == "OR"
    assert m["key"] == "gpt-5-nano" and m["in_mtok"] == 0.05   # /1k→/Mtok, strip "/" prefix
    print("  ✓ curate(openrouter): external/openai_compatible/OR, short key (split on /)")


def test_select_candidates_price_floor():
    """select_candidates: exclude LEGACY/missing-price, src mandatory, ascending price, max_price/max_n."""
    src = [{"key": "qwen-vl", "origin": "src", "model_id": "qwen.x", "call": "on_demand",
            "in_mtok": 0.53, "out_mtok": 2.66, "tier": "jp", "price_src": "B",
            "lifecycle": "ACTIVE", "quality_hint": "unknown", "note": ""}]
    models = models_cmd.curate(src, CATALOG_FIXTURE)
    keys, info = models_cmd.select_candidates(models)
    assert "qwen-vl" in keys, "src must always be included"
    # LEGACY (claude-3-haiku) and missing-price are excluded
    assert "claude-3-haiku-20240307-v1:0" not in str(keys)
    assert info["dropped_legacy"], "LEGACY detected"
    # ascending price (challengers after src in cheapest-first order)
    assert keys[0] == "qwen-vl"
    assert "gemma-3-4b" in keys and "kimi-k2.5" in keys
    # with max_price only gemma (0.04) stays, kimi (0.6) drops (src kept separately)
    k2, _ = models_cmd.select_candidates(models, max_price=0.1)
    assert "gemma-3-4b" in k2 and "kimi-k2.5" not in k2 and "qwen-vl" in k2
    print(f"  ✓ select_candidates: src mandatory / LEGACY excluded / ascending price / max_price cutoff ({info['n_chosen']} kinds)")


def test_select_candidates_min_price_floor():
    """[new] min_price excludes the "prior failure band (dirt-cheap/tiny = Japanese OCR wipeout)" from probing.

    Rationale = measured: nova-lite ($0.06)/gemma-3-4b ($0.04) fell below the floors and wiped out.
    With "cheapest N" the probe budget is wasted on this failure band → cut with a floor (keep src separately).
    """
    src = [{"key": "qwen-vl", "origin": "src", "model_id": "qwen.x", "call": "on_demand",
            "in_mtok": 0.53, "out_mtok": 2.66, "tier": "jp", "price_src": "B",
            "lifecycle": "ACTIVE", "quality_hint": "unknown", "note": ""}]
    models = models_cmd.curate(src, CATALOG_FIXTURE)  # gemma 0.04 / kimi 0.6 / legacy (no price)
    keys, info = models_cmd.select_candidates(models, min_price=0.1)
    assert "gemma-3-4b" not in keys, "dirt-cheap band (0.04) excluded at min_price=0.1"
    assert "kimi-k2.5" in keys, "0.6 stays at or above the floor"
    assert "qwen-vl" in keys, "src kept regardless of the floor (baseline)"
    assert "gemma-3-4b" in info["dropped_below_floor"]
    # without min_price, behaves as before (backward compatible = no regression)
    k0, info0 = models_cmd.select_candidates(models)
    assert "gemma-3-4b" in k0 and info0["dropped_below_floor"] == []
    print(f"  ✓ [new] min_price: failure band excluded / src kept / backward compatible ({len(keys)} kinds after floor)")


def test_select_stratified_spans_tiers():
    """[new] span price tiers (cheap~strong), both bedrock+openrouter, deterministic, max_price caps the top."""
    prices = [0.01, 0.03, 0.05, 0.08, 0.1, 0.2, 0.5, 1.0, 3.0, 5.0, 15.0, 30.0]
    models = []
    for i, p in enumerate(prices):
        origin = "bedrock" if i % 2 else "openrouter"
        models.append({"key": f"m{i}", "origin": origin, "model_id": f"{origin}.m{i}",
                       "call": "on_demand" if origin == "bedrock" else "openai_compatible",
                       "in_mtok": p, "out_mtok": p * 2, "tier": "x", "price_src": "B",
                       "lifecycle": "ACTIVE", "quality_hint": "unknown", "note": ""})
    models.append({"key": "cur", "origin": "src", "model_id": "src.cur", "call": "on_demand",
                   "in_mtok": 0.5, "out_mtok": 1.0, "tier": "jp", "price_src": "A",
                   "lifecycle": "ACTIVE", "quality_hint": "good", "note": ""})
    keys, info = models_cmd.select_stratified(models, bands=4, per_band=2)
    by = {m["key"]: m for m in models}
    assert "cur" in keys, "src must be included"
    cp = [by[k]["in_mtok"] for k in keys]
    assert min(cp) <= 0.05 and max(cp) >= 15.0, "both the cheap end and the expensive end = frontier"
    origins = {by[k]["origin"] for k in keys}
    assert "bedrock" in origins and "openrouter" in origins, "both bedrock and openrouter appear"
    assert models_cmd.select_stratified(models, bands=4, per_band=2)[0] == keys, "deterministic"
    k2, _ = models_cmd.select_stratified(models, bands=4, per_band=2, max_price=5.0)
    assert max(by[k]["in_mtok"] for k in k2) <= 5.0, "max_price excludes the high-price band"
    print(f"  ✓ select_stratified: cheap~strong span / bedrock+openrouter / deterministic / max_price ({len(keys)} kinds)")


def test_suggest_floor_deterministic():
    """[new] deterministically compute the failure-band floor from a past run + floors + prices (no AI quality guess)."""
    from nishiki import suggest_floor as sf
    run = {"candidates": [
        {"label": "CASCADE", "is_reference": True, "overall_acc": 1.0, "ng_recall": 1.0},
        {"label": "nova-lite", "overall_acc": 0.50, "ng_recall": 0.30},     # fail (both below)
        {"label": "gemma-3-4b", "overall_acc": 0.55, "ng_recall": 0.40},    # fail
        {"label": "qwen-vl", "overall_acc": 0.93, "ng_recall": 0.86},       # pass
        {"label": "kimi-k2.5", "overall_acc": 0.95, "ng_recall": 0.85},     # pass
    ]}
    floors = {"kpi_floor": 0.8, "ng_recall_floor": 0.7}
    prices = {"nova-lite": 0.06, "gemma-3-4b": 0.04, "qwen-vl": 0.53, "kimi-k2.5": 0.6}
    r = sf.compute_floor(run, floors, prices)
    assert {f["key"] for f in r["failed"]} == {"nova-lite", "gemma-3-4b"}
    assert {f["key"] for f in r["passed"]} == {"qwen-vl", "kimi-k2.5"}   # reference is out of scope
    assert r["cheapest_pass"] == 0.53 and r["max_failed_price"] == 0.06
    assert r["suggested_min_price"] == 0.53, "failure band is below → suggest the cheapest pass"
    # passing the suggested floor to select_candidates drops the failure band (integration)
    models = [
        {"key": "nova-lite", "origin": "bedrock", "model_id": "amazon.nova", "call": "on_demand",
         "in_mtok": 0.06, "out_mtok": 0.24, "tier": "ap-ne1", "lifecycle": "ACTIVE",
         "price_src": "B", "quality_hint": "trap", "note": ""},
        {"key": "kimi-k2.5", "origin": "bedrock", "model_id": "moonshotai.kimi", "call": "on_demand",
         "in_mtok": 0.6, "out_mtok": 3.0, "tier": "ap-ne1", "lifecycle": "ACTIVE",
         "price_src": "B", "quality_hint": "unknown", "note": ""},
    ]
    keys, _ = models_cmd.select_candidates(models, min_price=r["suggested_min_price"])
    assert "nova-lite" not in keys and "kimi-k2.5" in keys

    # no failures → no suggestion (= exclude nothing)
    run2 = {"candidates": [{"label": "qwen-vl", "overall_acc": 0.93, "ng_recall": 0.86}]}
    assert sf.compute_floor(run2, floors, {"qwen-vl": 0.53})["suggested_min_price"] is None
    print("  ✓ [new] suggest-floor: extract failures / suggest cheapest pass / integrated exclude / no failure = no suggestion")


def test_koi_structure_and_candidates():
    """build_koi_yaml structure + candidates (ALL / recommended list) rendering."""
    # ALL (no recommendation)
    g0 = yaml.safe_load(init_cmd.build_koi_yaml("t", "m:f"))
    assert g0["candidates"] == "ALL"
    assert g0["koi"]["formula"] == "kpi / cost_per_item"
    assert g0["residency_bar"] == "your_cloud"
    assert g0["floors"] == {"kpi_floor": 0.8, "ng_recall_floor": 0.7}

    # recommended list
    g1 = yaml.safe_load(init_cmd.build_koi_yaml(
        "example_target", "core.services.bedrock_client:converse",
        candidates=["qwen-vl", "claude-sonnet", "kimi-k2.5"],
        recommend_reason="画像OCR=vision対応のみ・LEGACY除外", gold_batch=6472))
    assert g1["candidates"] == ["qwen-vl", "claude-sonnet", "kimi-k2.5"]
    assert g1["gold"]["batch_id"] == 6472
    assert g1["injection"]["choke"] == "core.services.bedrock_client:converse"
    print("  ✓ KOI.yaml: structure + candidates (ALL / recommended list) both supported")


def test_spread_pick_spans_range():
    """[new] _spread_pick: pick n at even intervals from ascending price = span cheap~strong (not just the cheapest)."""
    items = [{"p": i} for i in range(100)]   # 0..99 (price proxy)
    picked = init_cmd._spread_pick(items, 5)
    vals = [it["p"] for it in picked]
    assert vals[0] == 0 and vals[-1] == 99, f"includes both ends (cheapest, strongest): {vals}"
    assert len(picked) == 5 and vals == sorted(vals)
    # not skewed to the cheap end (median is around the middle of the range)
    assert 40 <= vals[2] <= 60, f"middle is at range center: {vals}"
    # n >= count → all
    assert init_cmd._spread_pick(items[:3], 10) == items[:3]
    print(f"  ✓ _spread_pick: span cheap~strong at even intervals (e.g. {vals})")


def test_discover_generic_chokes():
    """[new] discover also picks up non-bedrock LLM calls (openai/anthropic/langchain) as chokes."""
    d = tempfile.mkdtemp(prefix="nishiki_disc_")
    with open(os.path.join(d, "agent.py"), "w", encoding="utf-8") as f:
        f.write(
            "import openai\n"
            "def classify(text):\n"
            "    r = client.chat.completions.create(model='gpt', messages=[{'x':1}])\n"
            "    return r\n"
            "def extract(ctx):\n"
            "    return llm.invoke(ctx)  # langchain\n")
    found = init_cmd.discover(d)
    blob = "\n".join(found["choke"])
    assert "chat.completions" in blob and ".invoke(" in blob, "generic chokes not picked up"
    print("  ✓ discover: also detects generic chokes like openai/langchain")


def test_discover_raw_http_chokes():
    """[new] discover also picks up agents that call the provider REST endpoint directly (no SDK).

    A raw urllib/requests/httpx POST to chat/completions or /v1/messages has no SDK method to match,
    so it's found by the endpoint PATH or the provider HOST instead.
    """
    d = tempfile.mkdtemp(prefix="nishiki_http_")
    with open(os.path.join(d, "raw_openai.py"), "w", encoding="utf-8") as f:
        f.write(
            "import urllib.request, json\n"
            "URL = 'https://openrouter.ai/api/v1/chat/completions'\n"
            "def extract_span(q, ctx):\n"
            "    req = urllib.request.Request(URL, data=json.dumps({}).encode())\n"
            "    return urllib.request.urlopen(req)\n")
    with open(os.path.join(d, "raw_anthropic.py"), "w", encoding="utf-8") as f:
        f.write(
            "import requests\n"
            "def run(p):\n"
            "    return requests.post('https://api.anthropic.com/v1/messages', json={'x': p})\n")
    found = init_cmd.discover(d)
    blob = "\n".join(found["choke"])
    assert "chat/completions" in blob, "raw-HTTP OpenAI/OpenRouter endpoint not picked up"
    assert "/v1/messages" in blob or "api.anthropic.com" in blob, "raw-HTTP Anthropic endpoint not picked up"
    print("  ✓ discover: also detects raw-HTTP chokes (urllib/requests to REST endpoints, no SDK)")


def test_koi_yaml_extraction_nonclassification():
    """[new] task_type=extraction (non-classification): mode2 / gold=file / span_f1 / kpi_floor only / no reference.

    The core of generic init being able to auto-generate KOI.yaml for a non-classification task (design doc §18.9).
    Classification (default) is unchanged = regression (pinned in the test above).
    """
    g = yaml.safe_load(init_cmd.build_koi_yaml(
        "cuad", "cuad_agent:extract_clause", task_type="extraction",
        gold_data="/data/CUADv1.json", candidates=["gpt-5-nano", "qwen3-vl"],
        cost_locus="LLM chat 1回"))
    assert g["task_type"] == "extraction" and g["mode"] == 2
    assert g["gold"]["source"] == "file" and g["gold"]["data"] == "/data/CUADv1.json"
    assert "batch_id" not in g["gold"] and "in_scope_verdicts" not in g["gold"]
    assert g["scorer"]["kpi"] == "span_f1"                 # task_type → scorer name auto-mapping
    assert g["floors"] == {"kpi_floor": 0.55}              # no ng_recall_floor
    assert "reference" not in g                            # new task with no current path
    assert g["injection"]["choke"] == "cuad_agent:extract_clause"
    # classification default is label_match / has ng_recall_floor / has reference (regression)
    gc = yaml.safe_load(init_cmd.build_koi_yaml("z", "m:f"))
    assert gc["task_type"] == "classification" and gc["scorer"]["kpi"] == "label_match"
    assert gc["floors"] == {"kpi_floor": 0.8, "ng_recall_floor": 0.7} and gc["reference"] == "CASCADE"
    print("  ✓ KOI.yaml (extraction): mode2/file/span_f1/kpi_floor only/no reference, classification unchanged")


def test_write_models_bedrock_regression():
    """[regression lock] Pin the existing bedrock single-source write_models_yaml behavior (detect regression).

    Pass catalog= to be net/boto3 independent. Guarantees src kept, bedrock added, src-first dedup,
    and absence of openai_compatible (= Bedrock only). **This behavior stays after going multi-source.**
    """
    tgt = _fake_target()
    out = tempfile.mkdtemp(prefix="nishiki_out_")
    models, cascade = init_cmd.write_models_yaml(out, tgt, source="bedrock",
                                                 catalog=CATALOG_FIXTURE)
    by = {m["key"]: m for m in models}
    assert cascade == ["qwen-vl", "claude-sonnet"], f"cascade: {cascade}"
    assert by["qwen-vl"]["origin"] == "src" and by["claude-sonnet"]["origin"] == "src"
    assert by["gemma-3-4b"]["origin"] == "bedrock" and by["kimi-k2.5"]["origin"] == "bedrock"
    # src qwen suppresses the same bedrock id (src-first dedup) = not duplicated
    assert sum(1 for m in models if m["model_id"] == "qwen.qwen3-vl-235b-a22b") == 1
    # bedrock only = no cross-border (openai_compatible) entry at all
    assert all(m["call"] != "openai_compatible" for m in models)
    # the written file is sound (round-trip)
    d = yaml.safe_load(open(os.path.join(out, "MODELS.yaml")))["models"]
    assert "qwen-vl" in d and "gemma-3-4b" in d
    assert all(v["origin"] in ("src", "bedrock") for v in d.values())
    print(f"  ✓ [regression] bedrock single-source: src kept + bedrock added + no cross-border ({len(models)} kinds)")


def test_write_models_multisource():
    """[new] with source='bedrock,openrouter' both sources coexist in one MODELS.yaml (no bedrock regression)."""
    tgt = _fake_target()
    out = tempfile.mkdtemp(prefix="nishiki_out_")
    with _patch_fetch_catalog(_OPENROUTER_FIXTURE):
        models, cascade = init_cmd.write_models_yaml(
            out, tgt, source="bedrock,openrouter", catalog=CATALOG_FIXTURE)
    by = {m["key"]: m for m in models}
    # the bedrock set stays unchanged (= no regression)
    assert by["gemma-3-4b"]["origin"] == "bedrock" and by["kimi-k2.5"]["origin"] == "bedrock"
    assert by["qwen-vl"]["origin"] == "src"
    # the openrouter set joins as openai_compatible / external
    assert by["gpt-5-nano"]["origin"] == "openrouter"
    assert by["gpt-5-nano"]["call"] == "openai_compatible" and by["gpt-5-nano"]["tier"] == "external"
    assert by["gpt-5-nano"]["in_mtok"] == 0.05    # per-1k→per-Mtok
    # price 0 (router/free) is excluded
    assert "free" not in by
    # YAML round-trip sound + 3 origins coexist
    d = yaml.safe_load(open(os.path.join(out, "MODELS.yaml")))["models"]
    assert {v["origin"] for v in d.values()} == {"src", "bedrock", "openrouter"}
    print(f"  ✓ [new] multi-source: src+bedrock+openrouter coexist ({len(models)} kinds, bedrock unchanged)")


def test_merge_source_additive():
    """[new] **additive merge** of openrouter into an existing (bedrock) profile. Keep existing bedrock/AGENT/KOI authoring."""
    tgt = _fake_target()
    out = tempfile.mkdtemp(prefix="nishiki_out_")
    # 1) create the existing profile (bedrock single)
    _models, cascade = init_cmd.write_models_yaml(out, tgt, source="bedrock", catalog=CATALOG_FIXTURE)
    with open(os.path.join(out, "KOI.yaml"), "w", encoding="utf-8") as f:
        f.write(init_cmd.build_koi_yaml(
            "faketgt", "core.services.bedrock_client:converse",
            candidates=["qwen-vl", "gemma-3-4b"], gold_batch=6472, cascade=cascade,
            how="LLM_MODELS を実行時に差し替える", cost_locus="添付 Vision のみ"))
    with open(os.path.join(out, "AGENT.md"), "w", encoding="utf-8") as f:
        f.write("ORIGINAL MAP — 手で書いた地図\n")
    n_before = len(yaml.safe_load(open(os.path.join(out, "MODELS.yaml")))["models"])

    # 2) additively merge openrouter (do not call AI authoring = keep existing AGENT/KOI authoring)
    with _patch_fetch_catalog(_OPENROUTER_FIXTURE):
        written = init_cmd.merge_source(out, "openrouter", target=tgt)

    # MODELS.yaml: existing bedrock/src remain + openrouter added (count grows)
    d = yaml.safe_load(open(os.path.join(out, "MODELS.yaml")))["models"]
    assert d["gemma-3-4b"]["origin"] == "bedrock", "existing bedrock not removed (no regression)"
    assert d["qwen-vl"]["origin"] == "src"
    assert any(v["origin"] == "openrouter" for v in d.values()), "openrouter was added"
    assert len(d) > n_before, f"count grew {n_before}→{len(d)}"
    # KOI.yaml: authoring fields kept + candidates recomputed (incl. openrouter) + residency released
    koi = yaml.safe_load(open(os.path.join(out, "KOI.yaml")))
    assert koi["injection"]["choke"] == "core.services.bedrock_client:converse", "choke kept"
    assert koi["gold"]["batch_id"] == 6472, "gold kept"
    assert "添付 Vision" in koi["injection"]["cost_locus"], "cost_locus kept"
    assert koi["residency_bar"] == "unrestricted", "adding a cross-border source releases the residency bar"
    assert any(k in ("gpt-5-nano", "llama-4-scout") for k in koi["candidates"]), "openrouter in candidates"
    # AGENT.md: do not touch at all (keep the existing map)
    assert open(os.path.join(out, "AGENT.md")).read() == "ORIGINAL MAP — 手で書いた地図\n"
    assert "MODELS.yaml" in written and "KOI.yaml" in written
    print(f"  ✓ [new] additive merge: bedrock kept + openrouter added ({n_before}→{len(d)}), KOI authoring/AGENT kept")


def test_koi_report_shows_latency():
    """[new] koi_report passes latency through to the HTML (prevents a regression where latency is missing from the table)."""
    import re
    from nishiki import koi_report
    payload = {"gold_batch": 6472, "n": 2, "gold_dist": {"OK": 1, "NG": 1}, "candidates": [
        {"label": "kimi", "is_reference": False, "overall_acc": 0.93, "ng_recall": 0.8,
         "ng_total": 1, "ng_miss_count": 0, "cost_per_item": 0.005, "errors": 0,
         "latency_ms_p50": 6600.0, "latency_ms_p95": 13000.0, "per_item": []},
        {"label": "CASCADE", "is_reference": True, "overall_acc": 0.93, "ng_recall": 0.8,
         "ng_total": 1, "ng_miss_count": 0, "cost_per_item": 0.013, "errors": 0,
         "latency_ms_p50": 11000.0, "latency_ms_p95": 28000.0, "per_item": []},
    ]}
    d = tempfile.mkdtemp(prefix="nishiki_koi_")
    rj, ky, out = (os.path.join(d, "r.json"), os.path.join(d, "KOI.yaml"), os.path.join(d, "o.html"))
    with open(rj, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    with open(ky, "w", encoding="utf-8") as f:
        f.write("floors: {kpi_floor: 0.8, ng_recall_floor: 0.7}\n")
    koi_report.generate(rj, ky, out)
    html = open(out, encoding="utf-8").read()
    assert "latency p50" in html, "latency column missing from header (= regression: latency not shown in the table)"
    D = json.loads(re.search(r'const D=(\{.*?\}),\$=id', html, re.S).group(1))
    by = {r["label"]: r for r in D["rows"]}
    assert by["kimi"]["p50"] == 6600.0 and by["CASCADE"]["p95"] == 28000.0
    print("  ✓ [new] koi_report: latency column + latency in payload (latency shows in the HTML)")


def test_koi_report_nonclassification():
    """[new] non-classification (ng_recall=None) = disable the NG floor and judge survival by kpi_floor alone.

    Extraction tasks like CUAD have no ng_recall. Currently surv required ng_recall!=null and
    wiped everything out → fix: candidates with ng_recall=None pass the NG floor, select by kpi_floor only.
    Classification (with ng_recall) is unchanged (regression = no degradation).
    """
    import re
    from nishiki import koi_report
    payload = {"n": 3, "candidates": [
        {"label": "m-good", "is_reference": False, "overall_acc": 0.85, "ng_recall": None,
         "ng_total": 0, "ng_miss_count": 0, "cost_per_item": 0.002, "errors": 0,
         "latency_ms_p50": 1200.0, "latency_ms_p95": 3000.0},
        {"label": "m-bad", "is_reference": False, "overall_acc": 0.40, "ng_recall": None,
         "ng_total": 0, "ng_miss_count": 0, "cost_per_item": 0.001, "errors": 0,
         "latency_ms_p50": 900.0, "latency_ms_p95": 2000.0},
    ]}
    d = tempfile.mkdtemp(prefix="nishiki_koi_nc_")
    rj, ky, out = (os.path.join(d, "r.json"), os.path.join(d, "KOI.yaml"), os.path.join(d, "o.html"))
    with open(rj, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    with open(ky, "w", encoding="utf-8") as f:
        f.write("floors: {kpi_floor: 0.8}\n")     # no ng_recall_floor = non-classification
    _path, best = koi_report.generate(rj, ky, out)
    # m-good (0.85≥0.8) survives, m-bad (0.40) drops on kpi_floor. No wipeout even without NG.
    assert best == "m-good", f"best={best} (check the NG floor is not wiping out non-classification)"
    html = open(out, encoding="utf-8").read()
    D = json.loads(re.search(r'const D=(\{.*?\}),\$=id', html, re.S).group(1))
    assert D.get("has_ng") is False, "non-classification has has_ng=False (hide the NG slider)"
    print("  ✓ [new] koi_report (non-classification): NG floor disabled, survive by kpi_floor only (no wipeout)")


def main():
    fails = 0
    for fn in (test_parse_src_facts, test_curate_full_list, test_curate_openrouter_origin,
               test_select_candidates_price_floor, test_select_candidates_min_price_floor,
               test_select_stratified_spans_tiers,
               test_suggest_floor_deterministic, test_koi_structure_and_candidates,
               test_spread_pick_spans_range, test_discover_generic_chokes,
               test_discover_raw_http_chokes,
               test_koi_yaml_extraction_nonclassification,
               test_write_models_bedrock_regression, test_write_models_multisource,
               test_merge_source_additive, test_koi_report_shows_latency,
               test_koi_report_nonclassification):
        try:
            print(f"[{fn.__name__}]")
            fn()
        except Exception as e:  # noqa: BLE001 - count unexpected exceptions as FAIL too (run all tests)
            fails += 1
            print(f"  ✗ FAIL: {type(e).__name__}: {e}")
    print("\n" + ("✅ all tests PASS" if not fails else f"❌ {fails} FAIL"))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
