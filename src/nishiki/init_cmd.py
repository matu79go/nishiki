"""nishiki init — read an existing agent's code and bootstrap a Nishiki experiment (providers/pipeline).

Roles (design doc §18.1/§18.2):
  Discovery layer (this module) = heuristically find the "model-call choke point / model catalog + price /
      cascade definition / KPI-relevant decision logic" in the code, and extract only those spans.
  Orchestrator (claude -p, flat-rate, zero Bedrock charges) = from the extracted code, author providers.yaml +
      pipeline.yaml (single candidate + promotion ladder) + KPI proposal (type A/B classification).
      **Extract model names/prices from the given code** (don't let the AI pick from memory = avoid fabrication).

The human only confirms the "gold (type B)" at the end. No hand-writing of config.
"""
import json
import os
import re

from . import orchestrator

# Discovery hints (function-name/constant-name patterns). Kept loose to work generically.
# Covers BOTH SDK-style calls (openai/anthropic/bedrock/langchain/litellm/gemini) AND agents that
# hit the provider REST endpoints directly with raw urllib/requests/httpx (no SDK) — matched by the
# LLM endpoint PATH (chat/completions, /v1/messages, …) or the provider HOST.
_CHOKE_HINTS = re.compile(
    r"bedrock-runtime|\.converse\(|chat\.completions|messages\.create|"
    r"GenerativeModel|client\.responses|InvokeModel|"
    r"litellm\.|\.invoke\(|\.ainvoke\(|\.generate\(|ChatOpenAI|ChatAnthropic|"
    r"openai\.|anthropic\.|\.complete\(|llm\(|generate_content|generateContent|"
    # raw-HTTP endpoints (no SDK): REST paths + provider hosts
    r"chat/completions|/v1/messages|/v1/complete|/v1/responses|"
    r"api\.openai\.com|openrouter\.ai|api\.anthropic\.com|generativelanguage\.googleapis|"
    r"api\.mistral\.ai|api\.groq\.com|api\.deepseek\.com|api\.together|api\.perplexity",
    re.I,
)
_PRICING_HINTS = re.compile(r"price|pricing|per_1k|per_mtok|per_1m|_USD|cost_per", re.I)
_CASCADE_HINTS = re.compile(r"cascade|escalat|ladder|fallback|accept", re.I)
# Model catalog (model_id / inference profile mapping). Always pick this up separately from pricing.
_CATALOG_HINTS = re.compile(
    r"MODELS\s*=|inference.?profile|model_id|\"jp\.|\"apac\.|amazon\.nova|"
    r"anthropic\.claude|qwen\.|:0\"",
    re.I,
)
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "target", "dist", "build"}


def _iter_py(root):
    for dpath, dnames, fnames in os.walk(root):
        dnames[:] = [d for d in dnames if d not in _SKIP_DIRS]
        for fn in fnames:
            if fn.endswith(".py"):
                yield os.path.join(dpath, fn)


def _window(lines, idx, before=2, after=12):
    a = max(0, idx - before)
    b = min(len(lines), idx + after)
    return a, b


def _snippets(path, pattern, root, max_hits=6):
    """Extract the lines around a pattern match (with relative path)."""
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    out, used = [], []
    rel = os.path.relpath(path, root)
    for i, ln in enumerate(lines):
        if pattern.search(ln):
            if any(a <= i < b for a, b in used):
                continue
            a, b = _window(lines, i)
            used.append((a, b))
            block = "\n".join(lines[a:b])
            out.append(f"# --- {rel}:{a+1}\n{block}")
            if len(out) >= max_hits:
                break
    return out


def discover(target):
    """Collect candidate choke / pricing / cascade snippets from the target project."""
    target = os.path.abspath(target)
    found = {"target": target, "choke": [], "catalog": [], "pricing": [], "cascade": []}
    for path in _iter_py(target):
        for key, pat in (("choke", _CHOKE_HINTS), ("catalog", _CATALOG_HINTS),
                         ("pricing", _PRICING_HINTS), ("cascade", _CASCADE_HINTS)):
            if len(found[key]) >= 8:
                continue
            found[key] += _snippets(path, pat, target)
    return found


def _bundle(found, cap=16000):
    """Combine discovered snippets into one code bundle (with a size cap)."""
    parts = []
    for key in ("choke", "catalog", "pricing", "cascade"):
        if found[key]:
            parts.append(f"## {key} candidates\n" + "\n\n".join(found[key][:6]))
    text = "\n\n".join(parts)
    return text[:cap]


SCHEMA = """\
You are a designer for Nishiki (the KOI optimizer). From the given "code fragments of an existing AI agent",
bootstrap the config needed to diagnose that agent with Nishiki. **Do not invent model names/prices not in the code**
(always extract from the given code; do not fill in from memory).

Output **exactly one strict JSON object** (no code fence, no prose). Keys:
{
  "choke": "module.path:func",         // the single function all model calls pass through (the watch target)
  "providers_yaml": "...",             // providers.yaml per the schema below (string)
  "pipeline_yaml": "...",              // pipeline.yaml per the schema below (string)
  "notes": "..."                       // KPI type A (verifiable) / type B (human required) classification, data the human must prepare/confirm, assumptions and open questions
}

# providers.yaml schema
providers:
  <model_key>:
    kind: bedrock_converse             // call type used at real calibrate (via choke). Use mock if unknown
    model_id: "<inference profile / model id from the code>"
    price: { in_per_1k: <num>, out_per_1k: <num>, unit: USD }   // convert from the code's price table (if per Mtok, /1000)

# If ALTERNATIVE_MODELS (optional, fetched live) is given:
#  - **always keep the current model (from the code) as the primary candidate**. Mix in only a few alternatives as "extra candidates".
#  - if you add alternatives, write one reason line each in NOTES (e.g. "same model cheaper via another route", "new cheap tier").
#  - if the given code targets a specific region (e.g. jp.-only / data-residency sensitive), then for adopting an external route (OpenRouter etc.)
#    **warn in NOTES that "data residency must be confirmed"** (do not silently make it the primary route).
#  - do not add models not in ALTERNATIVE_MODELS (do not invent from memory).

# pipeline.yaml schema
name: <short English name>
dataset: data/samples.jsonl
steps:
  - id: <id>
    type: model
    prompt: "<concise summary of the actual instruction inferable from the code. Be specific>"
    inputs:  [input.<field>]
    output:  <out>
    candidates:                        // mix a single model + a promotion ladder (cascade)
      - <model_key>                    // single
      - name: <ladder_name>            // ladder (cheap->strong, monotonic; only steps where accuracy rises; no trap models)
        cascade: [<cheap>, <strong>]
        accept: <rel/path.py:func>     // path of the promotion decision if present in code. Otherwise accept_conf: 1.0
  - id: verify
    type: deterministic
    run: rules/verify.py:check
    inputs:  [<...>]
    output:  verify_result
eval:
  kpi:
    scorer: builtin:label_accuracy
    pred: <final-step output>.<field>
    gold: expected.<field>
  kpi_floor: 0.90

# Rules
- the candidates "ladder" should propose only a few steps based on the code's cascade order (e.g. *_CASCADE_ORDER). Do not emit all permutations.
- candidates only on model steps. Matching/computation/verification is deterministic.
- frame the KPI as "compare the final output against expected". If type B (subjective) is mixed in, state it explicitly in notes.
"""


def _extract_json(text):
    # naively extract from the first { to its matching }
    start = text.find("{")
    if start < 0:
        raise ValueError("JSON not found")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise ValueError("JSON not closed")


def _alternative_models_block(vision=True):
    """Fetch alternative candidates live from OpenRouter into a small block for the prompt. Empty on failure."""
    try:
        from . import models_cmd
        catalog = models_cmd.fetch_catalog()
        tiers = models_cmd.suggest(catalog, vision=vision)
        picked = (tiers.get("high", [])[:2] + tiers.get("mid", [])[:2]
                  + tiers.get("cost", [])[:3])
        if not picked:
            return ""
        lines = [f"- {m['id']}  (in {m['in_per_1k']:.4g}/out {m['out_per_1k']:.4g} USD/1k"
                 + (", vision)" if m["vision"] else ")") for m in picked]
        return "\n# ALTERNATIVE_MODELS (fetched live, optional. OpenRouter = via openai_compatible)\n" + "\n".join(lines)
    except Exception:  # noqa: BLE001 - fetch failure is not fatal (continue with just the current model)
        return ""


def generate(target, timeout=240, suggest_models=False, backend=None, model=None):
    """Discover -> orchestrator brain (default OpenRouter) -> return config JSON.

    When backend/model are omitted, uses orchestrator.default_backend() (OpenRouter if a key
    is present, otherwise dev-only claude_p). suggest_models=True also proposes OpenRouter alternatives.
    """
    found = discover(target)
    bundle = _bundle(found)
    alt = _alternative_models_block() if suggest_models else ""
    prompt = SCHEMA + "\n\n# code fragments provided\n" + bundle + alt + "\n\nGenerate JSON from the above."
    text = orchestrator.call(prompt, backend=backend, model=model, timeout=timeout)
    obj = json.loads(_extract_json(text))
    return obj, found


def build_koi_yaml(target_name, choke, *, gold_batch=None, cascade=None,
                   candidates=None, recommend_reason=None,
                   in_scope_verdicts=("OK", "REVIEW", "NG"), cost_locus=None,
                   how=None, residency_bar="your_cloud", kpi=None,
                   kpi_floor=None, ng_recall_floor=0.70,
                   task_type="classification", gold_data=None, mode=None,
                   reference="CASCADE", run=None):
    """Generate the target's Nishiki run spec KOI.yaml (design doc §18.9). **Branch on task_type for classification/non-classification**.

    task_type="classification" (default, example_target family): gold=current verdict (mode1) / scoring=label_match /
      floors=kpi_floor + ng_recall_floor / reference=current route (1.0x anchor). **Preserve existing output** (regression-locked).
    task_type="extraction" (CUAD family): gold=dataset file (mode2) / scoring=span_f1 /
      floors=kpi_floor only / no reference (new task with no current route = rank by absolute quality).

    Deterministic part = defaults for scoring/KOI formula/floors/candidates/residency. Target-dependent (gold/choke/cost_locus)
    comes via arguments (= in generic init, the AI reads the source and decides/fills task_type/kpi/gold).
    """
    from . import scoring  # task_type -> default scorer name mapping
    is_cls = task_type == "classification"
    if kpi is None:
        kpi = scoring.TASK_SCORER.get(task_type, "label_match")
    if mode is None:
        mode = 1 if is_cls else 2
    if kpi_floor is None:
        kpi_floor = 0.80 if is_cls else 0.55
    if not is_cls:
        reference = None
    _ = cascade  # accepted only (current is not emitted in the body; backward compat)

    L = [
        f"# KOI.yaml — Nishiki run spec for target {target_name} (canonical). Auto-generated by nishiki init.",
        "# The blueprint for \"how Nishiki measures this target\" (design doc §18.9).",
        "#",
        "# ▼ KOI = achieved KPI ÷ cost = quality per dollar spent (AI-grade cost-efficiency/ROI).",
        "# ▼ floors (cutoff) = the must-hit quality line. Kills the \"cheaper is better\" trap and surfaces the most cost-efficient among those that pass.",
        "",
        f"target: {target_name}",
        f"task_type: {task_type}        # classification=classification (match rate) / extraction=extraction (span F1)",
    ]
    if is_cls:
        L += [
            "mode: 1                      # 1=self-run (gold=current model's verdict). 2=human labels",
            "",
            "# ★ceiling (mode1): gold=current output -> current=1.0x=[upper bound]. What we can measure is 'current-equivalent for cheaper'.",
            "",
            "# ── gold (reference data) ──────────────────────────────",
            "gold:",
            "  source: batch              # use the existing batch's verdicts as gold (zero re-run cost)",
            f"  batch_id: {gold_batch if gold_batch is not None else 'TBD  # <- gold batch ID'}",
            f"  in_scope_verdicts: [{', '.join(in_scope_verdicts)}]",
            "  read_only: true            # target DB is SELECT only",
        ]
    else:
        L += [
            f"mode: {mode}                      # 2=human labels (the dataset's gold annotations)",
            "",
            "# new task = no current route -> measure by absolute quality (match against gold). No 1.0x vs current.",
            "",
            "# ── gold (reference data) = gold annotations (no data stored in the repo) ──",
            "gold:",
            "  source: file               # gold dataset (SQuAD format etc.)",
            f"  data: {gold_data or 'TBD  # <- path to the gold dataset (e.g. on your data host)'}",
            "  read_only: true",
        ]
    L += [
        "",
        "# ── injection point (choke point) = where candidate models are spliced in (runtime memory only; source unchanged) ──",
        "injection:",
        f"  choke: {choke or 'TBD'}",
    ]
    if how:
        L.append("  how: |")
        L += [f"    {ln}" for ln in how.strip().splitlines()]
    if cost_locus:
        L.append(f"  cost_locus: {cost_locus}")
    if run:
        # GenericAdapter run config. With this present, the target can be measured without target-specific glue.
        L += [
            "",
            "# ── generic adapter run config (read by GenericAdapter; no target-specific glue needed) ──",
            "run:",
            f"  gold_format: {run.get('gold_format', 'jsonl')}   # loader: squad / jsonl",
            f"  parser: {run.get('parser', 'identity')}        # response->pred: locate_spans / identity / label",
        ]
        if run.get("balance"):
            L.append("  balance: true")
        L.append("  prompt: |")
        L += [f"    {ln}" for ln in str(run.get("prompt", "")).strip().splitlines()]
    L += ["", "# ── candidates (what we measure this run. Full menu is in MODELS.yaml) ────────────────"]
    if candidates:
        if recommend_reason:
            L.append(f"# selection policy: {recommend_reason}")
        L.append("# deterministic selection by price cutoff/stratification (quality is decided by probe=measured). Add/remove freely.")
        L.append("candidates: [" + ", ".join(candidates) + "]")
    else:
        L.append("candidates: ALL              # ALL / NEW / KNOWN / [explicit keys...]")
    if reference:
        L.append(f"reference: {reference}           # baseline that anchors KOI to 1.0x (the current route)")
    L += [
        "",
        f"residency_bar: {residency_bar}   # your_cloud=exclude external routes before the real run (sensitive data) / unrestricted=allow",
        "",
        "# ── scoring + KOI = \"KPI ÷ cost\" + cutoff ──",
        "scorer:",
        f"  kpi: {kpi}         # scoring registry name (label_match=classification match / span_f1=extraction span F1)",
        "",
        "koi:",
        '  formula: "kpi / cost_per_item"   # passers of the cutoff only. Ordered by cheapest-for-the-quality',
        "",
        "# ── cutoff (SLA) = dynamically selected via the UI slider. Candidates that fail are disqualified ──",
        "floors:",
        f"  kpi_floor: {kpi_floor}            # KPI floor (must-hit line)",
    ]
    if is_cls:
        L.append(f"  ng_recall_floor: {ng_recall_floor}      # floor on NG-detection rate (classification safety = missing an NG hurts)")
    else:
        L.append("  # non-classification uses kpi_floor only (ng_recall_floor is classification-specific)")
    return "\n".join(L) + "\n"


def _split_sources(source):
    """Turn a source string (comma-separated allowed) into a list of source names. 'bedrock,openrouter' -> ['bedrock','openrouter']."""
    return [s.strip() for s in (source or "").split(",") if s.strip()]


def _spread_pick(items, n):
    """From price-ascending items, evenly pick n across the whole price range (include cheap~strong)."""
    if n is None or n >= len(items):
        return list(items)
    if n <= 1:
        return items[:1]
    step = (len(items) - 1) / (n - 1)
    idx = sorted({round(i * step) for i in range(n)})
    return [items[j] for j in idx]


def _fetch_source_catalog(source, *, region="ap-northeast-1", openrouter_cap=30, catalog=None):
    """Fetch one source's candidate catalog and return (extra, origin) (in the form curate expects).

    bedrock = fetch_bedrock_catalog (real enumeration; needs boto3 auth) / openrouter = fetch_catalog (public; no key needed).
    Pass catalog (pre-fetched for bedrock) to skip the bedrock fetch (fetch=inside container / assembly=on host).
    A fetch failure (no auth etc.) is non-fatal and returns empty (the caller can continue with src/existing only).
    """
    from . import models_cmd
    if source == "bedrock":
        if catalog is not None:
            return list(catalog), "bedrock"
        try:
            return models_cmd.fetch_bedrock_catalog(region=region, vision_only=True), "bedrock"
        except Exception as e:  # noqa: BLE001 - continue empty in environments without auth/boto3
            print(f"  ⚠ Bedrock catalog fetch failed ({type(e).__name__}): continuing with this source empty.")
            return [], "bedrock"
    if source == "openrouter":
        try:
            full = models_cmd.fetch_catalog()                       # public, no key, no charges
            # Pick across the price range (cheap-only = can't draw the frontier). Exclude extreme outliers
            # (o1-pro $150 etc.) and include up to claude/gpt-5/opus class ($<=15/Mtok).
            MAX_PER_1K = 0.015                                      # = $15/Mtok cap
            vis = sorted((m for m in full if m.get("vision")
                          and 0 < m.get("in_per_1k", 0) <= MAX_PER_1K),
                         key=lambda m: m["in_per_1k"])
            extra = _spread_pick(vis, openrouter_cap)               # cheap~strong evenly spaced
            lo = extra[0]["in_per_1k"] * 1000 if extra else 0
            hi = extra[-1]["in_per_1k"] * 1000 if extra else 0
            print(f"  OpenRouter: from {len(vis)} paid vision models (<=$15/Mtok), menu-ize "
                  f"{len(extra)} across the price range (${lo:.2f}~${hi:.2f}/Mtok, cross-border=tier external). "
                  f"Adjust the cap with --openrouter-cap.")
            return extra, "openrouter"
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠ OpenRouter catalog fetch failed ({type(e).__name__}): continuing with this source empty.")
            return [], "openrouter"
    raise ValueError(f"unknown source: {source!r} (bedrock / openrouter)")


def write_models_yaml(out_dir, target, source="bedrock", region="ap-northeast-1",
                      catalog=None, openrouter_cap=30):
    """Deterministically generate and write MODELS.yaml from target code (src) + selected sources (multiple allowed, comma-separated).

    src = parse_src_models(target code), always kept. Each source is **additively merged** via curate
    (e.g. source="bedrock,openrouter" -> unify src + bedrock enumeration + openrouter enumeration into one).
    Duplicates (same model_id / key) are first-wins = src priority. Pass catalog to skip the bedrock fetch.
    Returns (models, cascade). If a source fails to fetch, it is skipped empty and processing continues.
    """
    from . import models_cmd
    import os
    src, cascade = models_cmd.parse_src_models(target)
    models = list(src)
    for s in _split_sources(source):
        extra, origin = _fetch_source_catalog(
            s, region=region, openrouter_cap=openrouter_cap,
            catalog=catalog if s == "bedrock" else None)
        models = models_cmd.curate(models, extra, origin=origin)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "MODELS.yaml"), "w", encoding="utf-8") as f:
        f.write(models_cmd.to_models_yaml(models, cascade))
    return models, cascade


def load_models_yaml(path):
    """Restore an existing MODELS.yaml into a list of internal model dicts (the base for additive merge).

    The inverse of to_models_yaml. Maps YAML keys in/out -> in_mtok/out_mtok, life -> lifecycle.
    Empty list if the file is missing.
    """
    import os
    if not os.path.exists(path):
        return []
    import yaml
    doc = yaml.safe_load(open(path, encoding="utf-8")) or {}
    out = []
    for key, m in (doc.get("models") or {}).items():
        out.append({
            "key": key,
            "origin": m.get("origin", "src"),
            "model_id": m["model_id"],
            "call": m.get("call", "on_demand"),
            "in_mtok": m.get("in"),
            "out_mtok": m.get("out"),
            "tier": m.get("tier"),
            "price_src": m.get("price_src"),
            "lifecycle": m.get("life", "ACTIVE"),
            "quality_hint": m.get("quality_hint", "unknown"),
            "note": m.get("note", "") or "",
        })
    return out


def merge_source(out_dir, source, *, target=None, region="ap-northeast-1",
                 openrouter_cap=30, max_price=None, min_price=None, max_n=None,
                 catalog=None):
    """**Additively merge** a new source's candidates into an existing profile (out_dir) (production use = don't break the existing).

    Design (2026-06-21, degradation prevention): don't redo the whole init; rewrite only the bare minimum.
      - MODELS.yaml: add the new source **while keeping** existing candidates (src/bedrock etc.). Duplicates are first-wins.
      - KOI.yaml: **preserve** AI-authored fields (choke/how/cost_locus/gold/floors/kpi), and update only
        candidates (recomputed price cutoff = including the new source) and residency_bar (relaxed when a cross-border source is added).
      - AGENT.md: untouched (preserve the existing map). No AI authoring (no charges) is run.
    Returns a dict of updated file paths.
    """
    from . import models_cmd
    import os
    import yaml
    models_path = os.path.join(out_dir, "MODELS.yaml")
    existing = load_models_yaml(models_path)
    if not existing:
        raise FileNotFoundError(
            f"no existing MODELS.yaml: {out_dir} (run nishiki init --source first)")
    extra, origin = _fetch_source_catalog(
        source, region=region, openrouter_cap=openrouter_cap,
        catalog=catalog if source == "bedrock" else None)
    models = models_cmd.curate(existing, extra, origin=origin)
    # cascade is for the comment footnote. If target is given, re-derive from src (omit otherwise).
    cascade = None
    if target:
        try:
            _src, cascade = models_cmd.parse_src_models(target)
        except Exception:  # noqa: BLE001
            cascade = None
    with open(models_path, "w", encoding="utf-8") as f:
        f.write(models_cmd.to_models_yaml(models, cascade))
    written = {"MODELS.yaml": models_path}

    # update KOI.yaml preserving authored fields (recompute candidates + relax residency)
    koi_path = os.path.join(out_dir, "KOI.yaml")
    if os.path.exists(koi_path):
        koi = yaml.safe_load(open(koi_path, encoding="utf-8")) or {}
        cand, sel = models_cmd.select_candidates(
            models, max_price=max_price, min_price=min_price, max_n=max_n)
        inj = koi.get("injection") or {}
        gold = koi.get("gold") or {}
        floors = koi.get("floors") or {}
        scorer = koi.get("scorer") or {}
        pr = (f"${sel['price_min']:g}~${sel['price_max']:g}/Mtok"
              if sel["price_min"] is not None else "no price info")
        sel_note = (f"price cutoff (deterministic, after adding {source}): {sel['n_chosen']} models"
                    f"(src base {sel['n_src']} + challengers {sel['n_challenger']}, {pr}). Quality is decided by probe")
        # adding a cross-border source (openrouter) relaxes the residency bar (user explicitly chose the external route).
        residency_bar = "unrestricted" if origin == "openrouter" else koi.get("residency_bar", "your_cloud")
        new_koi = build_koi_yaml(
            koi.get("target") or "target",
            inj.get("choke"),
            cascade=cascade,
            candidates=cand or None,
            recommend_reason=sel_note,
            residency_bar=residency_bar,
            gold_batch=gold.get("batch_id"),
            in_scope_verdicts=tuple(gold.get("in_scope_verdicts") or ("OK", "REVIEW", "NG")),
            cost_locus=inj.get("cost_locus"),
            how=inj.get("how"),
            kpi=scorer.get("kpi") or "overall_acc",   # KPI = the RATE slot (agree/n), not the raw count
            kpi_floor=floors.get("kpi_floor", 0.80),
            ng_recall_floor=floors.get("ng_recall_floor", 0.70),
        )
        with open(koi_path, "w", encoding="utf-8") as f:
            f.write(new_koi)
        written["KOI.yaml"] = koi_path
    return written


# ── ③ AGENT.md authoring + AI inference of profile fields ───────────────────────────────
PROFILE_SCHEMA = """\
You are a domain analyst for Nishiki (the KOI optimizer). From the given "code fragments of the target AI agent" and
"the auto-extracted candidate model list (MODELS.yaml)", bootstrap a **domain profile** for measuring this target with Nishiki.

**Iron rule**: use only facts/evidence written in the code. **Do not invent from memory**. The given MODELS.yaml is authoritative for prices/model names.

Output **exactly one strict JSON object** (no code fence, no prose). Keys:
{
  "agent_md": "...",        // AGENT.md body (Markdown). The source map = structure / business flow / choke point /
                            //   location of gold / KPI definition / cost structure, at a granularity that lets you grasp it next time without re-reading the source.
  "choke": "module.path:func",   // the single function all model calls pass through (the injection point for swapping candidates)
  "cost_locus": "...",      // where charges occur (e.g.: attached Vision only; embed extraction/decision are free)
  "how": "...",             // steps to inject a candidate model at runtime (which constant/dict to swap). With code evidence
  "task_type": "classification|extraction",  // ★auto-detect by reading the source:
                            //   classification = emit one of a fixed label set (verdict/allow-deny/category) -> match rate
                            //   extraction     = extract/generate the relevant span from text (clause extraction, summarization etc.) -> span F1
  "gold": {                 // how to build gold (reference data)
    "source": "batch|file|...",  // existing output (batch) / gold dataset (file)
    "data": "<path if file. TBD if unknown>",
    "in_scope_verdicts": ["..."],  // the set of gold labels to score (when classification)
    "note": "how gold is identified. TBD if batch_id/path etc. can't be fixed without seeing the real data"
  },
  "run": {                  // ★run config for measuring with non-target-specific glue (required for extraction/generic)
    "gold_format": "squad|jsonl",   // format of the gold dataset
    "parser": "locate_spans|identity|label",  // response->pred (extraction=locate_spans / generation=identity / classification=label)
    "prompt": "<prompt template passed to the candidate model. **Expand the dataset's field names with {field}**."
              // ★field names are determined by gold_format: squad -> {question} and {context} (full contract text). "
              // jsonl -> each key of that JSON. Follow the wording of the real prompt in the code, but match the {field} names to this>"
  },
  "kpi": null,              // usually null (auto from task_type: classification->label_match / extraction->span_f1).
                            //   set explicitly only when there is a special scoring name (one of label_match / span_f1)
  "model_eval": {           // for each key in MODELS.yaml, attach known evaluations from the code's comments/evidence
    "<model_key>": { "quality_hint": "good|trap|unknown", "note": "short note from code evidence (reason for trap etc.). Empty if none" }
  }
}
# ★Do not let the AI select candidates (probe targets). Select deterministically by price cutoff (quality trade-offs are decided by probe=measured).
#   The AI's job is only the "map (agent_md) / injection point (choke/how) / cost locus / how to build gold / quality_hint from comment evidence".
# Notes:
# - in model_eval, attach good/trap only to entries that have evaluation evidence in the code (comments etc.).
#   With no evidence, leave quality_hint=unknown (confirm via probe = don't lie).
"""


def author_profile(target, models, *, backend=None, model=None, timeout=240):
    """Pass the target code + MODELS.yaml to the orchestrator to author the domain profile JSON."""
    found = discover(target)
    bundle = _bundle(found)
    from . import models_cmd
    models_yaml = models_cmd.to_models_yaml(models)
    prompt = (PROFILE_SCHEMA + "\n\n# target code fragments\n" + bundle
              + "\n\n# auto-extracted MODELS.yaml\n" + models_yaml
              + "\n\nGenerate JSON from the above.")
    text = orchestrator.call(prompt, backend=backend, model=model, timeout=timeout)
    return json.loads(_extract_json(text)), found


def apply_model_eval(models, model_eval):
    """Apply the AI-authored quality_hint/note to the MODELS.yaml models (only those with evidence)."""
    if not model_eval:
        return models
    for m in models:
        ev = model_eval.get(m["key"])
        if not isinstance(ev, dict):
            continue
        jp = ev.get("quality_hint")
        if jp in ("good", "trap", "unknown"):
            m["quality_hint"] = jp
        if ev.get("note"):
            m["note"] = ev["note"]
    return models


def generate_profile(target, source="bedrock", out_dir="exp", *, backend=None,
                     model=None, region="ap-northeast-1", timeout=240, author=True,
                     catalog=None, max_price=None, min_price=None, max_n=None,
                     openrouter_cap=30):
    """Auto-assembler: generate the full set of MODELS.yaml (deterministic) + AGENT.md/KOI.yaml (AI-authored).

    With author=False, don't call the AI and write only the deterministic part (MODELS.yaml + KOI.yaml skeleton).
    Returns a dict of generated file paths.
    """
    import os
    os.makedirs(out_dir, exist_ok=True)
    # target name = directory name. For generic names like app/src, take the parent (e.g. example_target/app -> example_target)
    ap = os.path.abspath(target).rstrip("/")
    target_name = os.path.basename(ap) or "target"
    if target_name in ("app", "src", "source", "lib", "."):
        target_name = os.path.basename(os.path.dirname(ap)) or target_name

    models, cascade = write_models_yaml(out_dir, target, source=source, region=region,
                                        catalog=catalog, openrouter_cap=openrouter_cap)
    written = {"MODELS.yaml": os.path.join(out_dir, "MODELS.yaml")}
    # if a cross-border source (openrouter) is included, drop the residency bar (user explicitly chose the external route)
    residency_bar = "unrestricted" if "openrouter" in _split_sources(source) else "your_cloud"

    profile = {}
    if author:
        profile, _found = author_profile(target, models, backend=backend, model=model, timeout=timeout)
        apply_model_eval(models, profile.get("model_eval"))
        # rewrite MODELS.yaml after applying quality_hint/note
        from . import models_cmd
        with open(os.path.join(out_dir, "MODELS.yaml"), "w", encoding="utf-8") as f:
            f.write(models_cmd.to_models_yaml(models, cascade))
        agent_md = profile.get("agent_md")
        if agent_md:
            with open(os.path.join(out_dir, "AGENT.md"), "w", encoding="utf-8") as f:
                f.write(agent_md.rstrip() + "\n")
            written["AGENT.md"] = os.path.join(out_dir, "AGENT.md")

    # candidates (probe targets) = **deterministic** selection by price cutoff (no AI quality judgment = safe).
    # "probe everything measurable -> rank via KOI". Exclude only LEGACY/missing price; src is always included as the baseline.
    from . import models_cmd
    cand, sel = models_cmd.select_candidates(
        models, max_price=max_price, min_price=min_price, max_n=max_n)
    pr = (f"${sel['price_min']:g}~${sel['price_max']:g}/Mtok"
          if sel["price_min"] is not None else "no price info")
    floor_note = f", excluded below failure-band floor ${min_price:g}" if min_price is not None else ""
    sel_note = (f"price cutoff (deterministic): adopted {sel['n_chosen']} vision candidates by ascending price"
                f"(src base {sel['n_src']} + challengers {sel['n_challenger']}, {pr}{floor_note}). Quality is decided by probe")
    print(f"  ── candidate menu {sel['n_total']} models -> probe targets {sel['n_chosen']} models (price cutoff) ──")
    print(f"     targets: {', '.join(cand)}")
    print(f"     price range: {pr}")
    # cost estimate (so candidates can be chosen by dollar amount. Per-item tokens are assumed = real cost is fixed by probe measurement)
    IN_TOK, OUT_TOK, PROBE_N = 1500, 500, 3
    chosen_models = [m for m in models if m["key"] in cand and m["in_mtok"] is not None]
    per_item_sum = sum((IN_TOK * m["in_mtok"] + OUT_TOK * m["out_mtok"]) / 1e6
                       for m in chosen_models)  # total cost of reading one item across all candidates
    print(f"     estimated cost (one item ~{IN_TOK}in/{OUT_TOK}out tok assumed, all {len(chosen_models)} candidates):")
    print(f"       - per item ≈ ${per_item_sum:.3f}")
    print(f"       - probe ({PROBE_N} items each) ≈ ${per_item_sum * PROBE_N:.2f}")
    print(f"       - ★scored-run total ≈ ${per_item_sum:.3f} × gold count"
          f"(e.g. 30 items ≈ ${per_item_sum * 30:.2f} / 50 items ≈ ${per_item_sum * 50:.2f})")
    print("       * real cost is measured by probe -> fixes the scored-run amount. Always show the total at the gate before billing.")
    if sel.get("dropped_below_floor"):
        print(f"     excluded (below failure-band floor ${min_price:g}, {len(sel['dropped_below_floor'])}): "
              f"{', '.join(sel['dropped_below_floor'])}  <- ultra-cheap band that doesn't deliver quality by precedent")
    if sel["dropped_legacy"]:
        print(f"     excluded (LEGACY {len(sel['dropped_legacy'])}): {', '.join(sel['dropped_legacy'])}")
    if sel["dropped_noprice"]:
        print(f"     excluded (price needed {len(sel['dropped_noprice'])}): {', '.join(sel['dropped_noprice'])}")
    if sel.get("dropped_dup"):
        print(f"     excluded (same model via another route {len(sel['dropped_dup'])}): {', '.join(sel['dropped_dup'])}")
    print("     -> real cost is fixed at calibrate's billing gate. Adjustable via candidates in KOI.yaml.")

    gold = profile.get("gold") or {}
    # ★task_type is auto-detected by the AI from the source (classification / extraction). If kpi is None,
    # it is auto-mapped from task_type (label_match / span_f1). For extraction, run (prompt/parser/gold_format)
    # yields a KOI.yaml the generic adapter can run without target-specific glue.
    task_type = profile.get("task_type") or "classification"
    koi = build_koi_yaml(
        target_name,
        profile.get("choke"),
        cascade=cascade,
        candidates=cand or None,
        recommend_reason=sel_note,
        residency_bar=residency_bar,
        gold_batch=gold.get("batch_id"),
        gold_data=gold.get("data"),
        in_scope_verdicts=tuple(gold.get("in_scope_verdicts") or ("OK", "REVIEW", "NG")),
        cost_locus=profile.get("cost_locus"),
        how=profile.get("how"),
        kpi=profile.get("kpi"),
        task_type=task_type,
        run=profile.get("run"),
    )
    with open(os.path.join(out_dir, "KOI.yaml"), "w", encoding="utf-8") as f:
        f.write(koi)
    written["KOI.yaml"] = os.path.join(out_dir, "KOI.yaml")
    return written


def write_experiment(out_dir, obj):
    """Write the generated providers/pipeline/notes out to the experiment directory (for review)."""
    os.makedirs(os.path.join(out_dir, "data"), exist_ok=True)
    os.makedirs(os.path.join(out_dir, "rules"), exist_ok=True)
    with open(os.path.join(out_dir, "providers.yaml"), "w", encoding="utf-8") as f:
        f.write(obj.get("providers_yaml", "").strip() + "\n")
    with open(os.path.join(out_dir, "pipeline.yaml"), "w", encoding="utf-8") as f:
        f.write(obj.get("pipeline_yaml", "").strip() + "\n")
    with open(os.path.join(out_dir, "NOTES.md"), "w", encoding="utf-8") as f:
        f.write(f"# init notes\n\nchoke point: `{obj.get('choke','?')}`\n\n{obj.get('notes','')}\n")
    return out_dir
