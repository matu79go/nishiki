"""nishiki models — live-fetch a provider's /models to propose candidate models (design doc §18.2 v2).

Key points:
- **Don't let the AI choose from memory**. Even the newest models released after training are
  fetched by hitting the API on the spot → zero fabrication.
- Listing is **not inference = no charges, no API key needed** (OpenRouter `GET /api/v1/models` is public).
- Price metadata comes attached → **the KOI denominator is filled in directly**.
- No brute force: filter by capability (vision etc.), compress into 3 tiers (high/mid/cost-efficient) to
  propose. Final selection is up to the human/orchestrator.
- OpenRouter is OpenAI-compatible → the output providers.yaml can be replayed as-is by the existing
  `openai_compatible` adapter.

Only the real calibrate step needs OPENROUTER_API_KEY (fetching/proposing in this module does not).
"""
import json
import urllib.request

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1"

# ── Bedrock price table (USD / 1M tok, in,out). The API has no prices, so this is hardcoded (source:
#    AWS Bedrock pricing, US-East list, ap-northeast needs confirming / claude=Anthropic published).
#    Looked up by modelId prefix match. Unlisted models return price=None (=needs a price set; no
#    fabrication). As of 2026-06-19.
BEDROCK_PRICING = {
    "amazon.nova-2-lite":            (0.06, 0.24),
    "amazon.nova-lite":              (0.06, 0.24),
    "amazon.nova-pro":               (0.80, 3.20),
    "qwen.qwen3-vl":                 (0.53, 2.66),
    "anthropic.claude-haiku-4-5":    (1.00, 5.00),
    "anthropic.claude-sonnet-4-6":   (3.00, 15.00),
    "anthropic.claude-sonnet-4-5":   (3.00, 15.00),
    "anthropic.claude-opus-4-8":     (5.00, 25.00),
    "anthropic.claude-opus-4-7":     (5.00, 25.00),
    "anthropic.claude-opus-4-6":     (5.00, 25.00),
    "google.gemma-3-4b":             (0.04, 0.08),
    "google.gemma-3-12b":            (0.09, 0.29),
    "google.gemma-3-27b":            (0.23, 0.38),
    "mistral.mistral-large-3":       (0.50, 1.50),
    "mistral.ministral-3-3b":        (0.10, 0.10),
    "mistral.ministral-3-8b":        (0.15, 0.15),
    "mistral.ministral-3-14b":       (0.20, 0.20),
    "mistral.magistral-small":       (0.50, 1.50),
    "moonshotai.kimi-k2.5":          (0.60, 3.00),
    "nvidia.nemotron-nano-12b":      (0.20, 0.20),
}


def _bedrock_price(model_id):
    """Look up BEDROCK_PRICING by modelId prefix match. (None, None) if not found."""
    for k, v in BEDROCK_PRICING.items():
        if model_id.startswith(k):
            return v
    return (None, None)


def fetch_bedrock_catalog(region="ap-northeast-1", vision_only=True):
    """Enumerate Bedrock's available models (list_foundation_models) → normalized candidates
    (key=boto3 default resolution).

    Fetched from **the live catalog**, not the AI's memory (zero fabrication). converse support is
    decided from inferenceTypesSupported:
      ON_DEMAND → call modelId directly (call=on_demand) / INFERENCE_PROFILE only → needs profile (call=profile).
    Prices come from BEDROCK_PRICING (unlisted = None = needs price).
    """
    import boto3  # lazy import (only in environments that need boto3/AWS auth)
    bc = boto3.client("bedrock", region_name=region)
    out = []
    for m in bc.list_foundation_models().get("modelSummaries", []):
        ins = m.get("inputModalities") or []
        outs = m.get("outputModalities") or []
        if vision_only and not ("IMAGE" in ins and "TEXT" in outs):
            continue
        its = m.get("inferenceTypesSupported") or []
        if not its:
            continue  # cannot invoke directly (derivatives like :200k)
        call = "on_demand" if "ON_DEMAND" in its else "profile"
        pin, pout = _bedrock_price(m["modelId"])
        out.append({
            "id": m["modelId"],
            "name": m.get("modelName", m["modelId"]),
            "in_per_1k": None if pin is None else pin / 1000.0,   # per-Mtok → per-1k
            "out_per_1k": None if pout is None else pout / 1000.0,
            "vision": "IMAGE" in ins,
            "call": call,
            "lifecycle": (m.get("modelLifecycle") or {}).get("status"),
            "origin": "bedrock",
        })
    return out


def fetch_catalog(url=OPENROUTER_MODELS_URL, timeout=20):
    """Fetch and normalize the live model catalog (no key needed, no charges)."""
    req = urllib.request.Request(url, headers={"User-Agent": "nishiki"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r).get("data", [])
    out = []
    for m in data:
        pr = m.get("pricing", {}) or {}
        try:
            in_tok = float(pr.get("prompt") or 0)
            out_tok = float(pr.get("completion") or 0)
        except (TypeError, ValueError):
            in_tok = out_tok = 0.0
        modal = (m.get("architecture", {}) or {}).get("input_modalities") or []
        out.append({
            "id": m["id"],
            "name": m.get("name", m["id"]),
            "in_per_1k": in_tok * 1000.0,    # per-token → per-1k
            "out_per_1k": out_tok * 1000.0,
            "vision": "image" in modal,
            "context": m.get("context_length"),
        })
    return out


def suggest(catalog, vision=False, search=None, include_free=False):
    """Filter by capability, then compress by price into 3 tiers (high/mid/cost-efficient)."""
    cands = catalog
    if vision:
        cands = [m for m in cands if m["vision"]]
    if search:
        s = search.lower()
        cands = [m for m in cands if s in m["id"].lower() or s in m["name"].lower()]
    # router(-1) and :free are excluded by default (price 0 can't be a KOI denominator)
    paid = [m for m in cands if m["in_per_1k"] > 0]
    pool = paid if not include_free else cands
    pool = sorted(pool, key=lambda m: m["in_per_1k"])
    if not pool:
        return {"cost": [], "mid": [], "high": []}
    mid_i = len(pool) // 2
    return {
        "cost": pool[:3],                              # cheapest (cost-efficient candidates)
        "mid":  pool[max(0, mid_i - 1):mid_i + 2],     # mid-tier
        "high": pool[-3:][::-1],                        # top (assumed high-performance)
        "n_pool": len(pool),
    }


def parse_src_models(target):
    """Deterministically extract the current models wired into the target code (origin=src). Zero fabrication.

    Reads a `constants.py` that declares the model wiring **by naming convention** (generic, not tied to
    any one project): a `<NAME>_MODELS` dict (key -> model id), an optional `<NAME>_PRICING...` dict
    (key -> (in, out) $/Mtok), and an optional `<NAME>_CASCADE` list (the promotion order). Price sources
    (A)/(B) are picked up from end-of-line comments. Returns empty if not found (falls back to the legacy
    path where the orchestrator extracts models from code).
    """
    import ast
    import os

    path = _find_constants(target)
    if not path:
        return [], []
    with open(path, encoding="utf-8", errors="ignore") as f:
        src = f.read()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return [], []

    # Detect by naming convention (first match wins), so any project's *_MODELS / *_PRICING* / *_CASCADE
    # constants are read — not a hardcoded constant name.
    models_map, pricing_map, cascade = {}, {}, []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            name = getattr(tgt, "id", None)
            if not name:
                continue
            if name.endswith("_MODELS") and isinstance(node.value, ast.Dict) and not models_map:
                models_map = _literal_dict(node.value)
            elif "PRICING" in name and isinstance(node.value, ast.Dict) and not pricing_map:
                pricing_map = _literal_dict(node.value)
            elif name.endswith("_CASCADE") and not cascade:
                try:
                    cascade = ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    cascade = []
    if not models_map:
        return [], []

    price_src = _price_src_from_comments(src, pricing_map)
    out = []
    for key, model_id in models_map.items():
        pin, pout = pricing_map.get(key, (None, None))
        out.append({
            "key": key,
            "origin": "src",
            "model_id": model_id,
            "call": _call_of(model_id),
            "in_mtok": pin,
            "out_mtok": pout,
            "tier": _tier_of(model_id, origin="src"),
            "price_src": price_src.get(key),
            "lifecycle": "ACTIVE",        # wired-in current = treated as ACTIVE
            "quality_hint": "unknown",
            "note": "",
        })
    return out, list(cascade)


def _find_constants(target):
    """Search under target for a constants.py that declares a `<NAME>_MODELS` dict (None if not found)."""
    import os
    import re
    models_decl = re.compile(r"^\s*\w*_MODELS\s*=\s*\{", re.M)
    for dpath, dnames, fnames in os.walk(target):
        dnames[:] = [d for d in dnames if d not in
                     {".git", "node_modules", "__pycache__", ".venv", "venv"}]
        for fn in fnames:
            if fn != "constants.py":
                continue
            p = os.path.join(dpath, fn)
            try:
                with open(p, encoding="utf-8", errors="ignore") as f:
                    if models_decl.search(f.read()):
                        return p
            except OSError:
                continue
    return None


def _literal_dict(dict_node):
    """Naively convert an ast.Dict to a Python dict (only constant/tuple keys and values)."""
    import ast
    out = {}
    for k, v in zip(dict_node.keys, dict_node.values):
        try:
            out[ast.literal_eval(k)] = ast.literal_eval(v)
        except (ValueError, SyntaxError):
            continue
    return out


def _price_src_from_comments(src, pricing_map):
    """Pick up end-of-line comments `# (A)` / `# (B)` on price-definition lines → key→source. Unmarked is None."""
    import re
    out = {}
    for key in pricing_map:
        # find the line starting with "<key>": ... and extract (A)/(B)
        m = re.search(rf'^\s*["\']{re.escape(key)}["\']\s*:.*?#.*?\(([AB])\)', src, re.M)
        if m:
            out[key] = m.group(1)
    return out


def _call_of(model_id):
    """inference profile (jp./apac. prefix) = profile / otherwise = on_demand direct call."""
    return "profile" if model_id.startswith(("jp.", "apac.", "us.", "eu.")) else "on_demand"


def _tier_of(model_id, origin):
    """Data residency tier. jp./apac. = profile residency, bedrock enumeration = region (ap-ne1)."""
    if model_id.startswith("jp."):
        return "jp"
    if model_id.startswith("apac."):
        return "apac"
    if model_id.startswith(("us.", "eu.")):
        return model_id.split(".", 1)[0]
    return "jp" if origin == "src" else "ap-ne1"


def _short_key(model_id):
    """Turn bedrock's long modelId into a human-readable short key (machine approximation; AI refines later).

    Drops the vendor prefix and strips -instruct/-it and trailing version/build (-vN, -NNNN).
    e.g. google.gemma-3-4b-it → gemma-3-4b / mistral.magistral-small-2509 → magistral-small.
    """
    import re
    # remove vendor prefix (bedrock="vendor.id" / openrouter="vendor/id")
    name = model_id.split("/")[-1] if "/" in model_id else model_id.split(".", 1)[-1]
    for suf in ("-instruct", "-it"):
        if name.endswith(suf):
            name = name[: -len(suf)]
    name = re.sub(r"-v\d+$", "", name)           # trailing -v2 etc.
    name = re.sub(r"-\d{4,}$", "", name)         # trailing 2509 etc. (date/build)
    return name


def curate(src_models, catalog, *, origin="bedrock", vision_only=True):
    """Merge the src current models + the selected source enumeration into a normalized candidate list for MODELS.yaml.

    - src is always kept (the baseline). In the catalog, any model_id identical to src yields to src (dedup).
    - vision candidates only; unpriced (in/out=None) are also kept (= explicitly flags needs-price; no fabrication).
    - quality_hint/note are not set (unknown/empty). trap/good evaluation is left to the AI-authoring layer or probe.
    - origin: bedrock (Bedrock enumeration) / openrouter (cross-border = tier external, via openai_compatible).
    """
    out = list(src_models)
    seen_ids = {m["model_id"] for m in out}
    seen_keys = {m["key"] for m in out}
    for m in catalog:
        if vision_only and not m.get("vision", True):
            continue
        mid = m["id"]
        if mid in seen_ids:
            continue
        key = _short_key(mid)
        while key in seen_keys:               # avoid key collisions with a suffix
            key += "-x"
        seen_ids.add(mid)
        seen_keys.add(key)
        pin = None if m.get("in_per_1k") is None else round(m["in_per_1k"] * 1000.0, 6)
        pout = None if m.get("out_per_1k") is None else round(m["out_per_1k"] * 1000.0, 6)
        if origin == "openrouter":
            call, tier = "openai_compatible", "external"   # cross-border = outside data residency
            price_src = "OR" if pin is not None else None  # OpenRouter live price = authoritative
        else:
            call = m.get("call", _call_of(mid))
            tier = _tier_of(mid, origin=origin)
            price_src = "B" if pin is not None else None   # US-East list = needs confirming
        out.append({
            "key": key,
            "origin": origin,
            "model_id": mid,
            "call": call,
            "in_mtok": pin,
            "out_mtok": pout,
            "tier": tier,
            "price_src": price_src,
            "lifecycle": m.get("lifecycle"),
            "quality_hint": "unknown",
            "note": "" if pin is not None else "needs price (no price metadata in catalog)",
        })
    return out


def select_candidates(models, *, max_price=None, min_price=None, max_n=None,
                      include_unpriced=False):
    """Select probe targets **deterministically** (price cutoff). No AI quality judgment = safety net.

    Policy (settled 2026-06-20): "probe everything within reach → rank by KOI". Quality selection is left to probe.
      - vision is the catalog default (all candidates are vision). Do not drop by modality.
      - Drop only LEGACY (lifecycle other than ACTIVE) and unpriced (when include_unpriced=False).
      - Always include the src current models as the baseline (regardless of price/cap).
      - Filter the rest by price (in) ascending, max_price (cap $/Mtok) / max_n (count).

    min_price (floor, added 2026-06-21): exclude the **empirically observed failure band** (dirt-cheap,
      tiny models that lack quality on e.g. Japanese OCR and fall below floors) from probing. With just
      "cheapest N", there was a hole that wasted probe budget on the dirt-cheap band that was wiped out
      before (nova-lite $0.06 / gemma-3-4b $0.04 wiped out at 44-53% match rate). This is not AI quality
      guessing but **using an empirically measured failure line** = distinct from accidental wrong exclusion.
      src is exempt from the floor.
    Returns: (keys, info). info = adopted/dropped breakdown, price range.
    """
    src = [m for m in models if m["origin"] == "src"]
    rest = [m for m in models if m["origin"] != "src"]
    dropped_legacy = [m["key"] for m in rest
                      if m.get("lifecycle") and m["lifecycle"] != "ACTIVE"]
    dropped_noprice = [m["key"] for m in rest
                       if m["in_mtok"] is None and not include_unpriced]
    pool = [m for m in rest
            if (m.get("lifecycle") in (None, "ACTIVE"))
            and (m["in_mtok"] is not None or include_unpriced)]
    # failure-band floor: exclude the dirt-cheap band empirically shown to lack quality from probing (src exempt).
    dropped_below_floor = []
    if min_price is not None:
        dropped_below_floor = [m["key"] for m in pool
                               if m["in_mtok"] is not None and m["in_mtok"] < min_price]
        pool = [m for m in pool
                if not (m["in_mtok"] is not None and m["in_mtok"] < min_price)]
    if max_price is not None:
        pool = [m for m in pool if m["in_mtok"] is not None and m["in_mtok"] <= max_price]
    pool.sort(key=lambda m: (m["in_mtok"] is None, m["in_mtok"] or 0))
    if max_n is not None:
        pool = pool[:max_n]
    # Per-base-model dedup: different paths for the same model (jp. profile vs on_demand direct call, etc.)
    # are identical for quality measurement = double-probing is wasteful. Process src (baseline) first to keep it.
    chosen, seen_base, dropped_dup = [], set(), []
    for m in src + pool:
        base = _base_model(m["model_id"])
        if base in seen_base:
            dropped_dup.append(m["key"])
            continue
        seen_base.add(base)
        chosen.append(m)
    pool = [m for m in chosen if m["origin"] != "src"]   # recompute post-dedup challenger count for info
    priced = [m["in_mtok"] for m in chosen if m["in_mtok"] is not None]
    info = {
        "n_total": len(models), "n_chosen": len(chosen),
        "n_src": len([m for m in chosen if m["origin"] == "src"]), "n_challenger": len(pool),
        "dropped_legacy": dropped_legacy, "dropped_noprice": dropped_noprice,
        "dropped_dup": dropped_dup, "dropped_below_floor": dropped_below_floor,
        "price_min": min(priced) if priced else None,
        "price_max": max(priced) if priced else None,
    }
    return [m["key"] for m in chosen], info


def select_stratified(models, *, bands=4, per_band=3, max_price=None, min_price=None,
                      include_unpriced=False):
    """Select candidates **across** price layers (deterministic). Always covers cheap-to-strong = for the cost/quality frontier.

    Why: `select_candidates` (cheapest N) only measures the cheap end and can't draw "how much do you pay
    before quality plateaus" = the frontier. This function splits the price range into bands contiguous
    bands by count, and takes per_band from each band by price spread → always includes cheap/mid/strong.
    **No AI quality judgment** (drops only LEGACY/missing-price/price floor; keeps the 2026-06-20 safety net).
    src is always included. Quality selection is decided by probe = measurement. Returns = (keys, info).
    """
    src = [m for m in models if m["origin"] == "src"]
    rest = [m for m in models if m["origin"] != "src"]
    dropped_legacy = [m["key"] for m in rest
                      if m.get("lifecycle") and m["lifecycle"] != "ACTIVE"]
    pool = [m for m in rest
            if (m.get("lifecycle") in (None, "ACTIVE"))
            and (m["in_mtok"] is not None or include_unpriced)]
    if min_price is not None:
        pool = [m for m in pool if m["in_mtok"] is None or m["in_mtok"] >= min_price]
    if max_price is not None:
        pool = [m for m in pool if m["in_mtok"] is None or m["in_mtok"] <= max_price]
    pool.sort(key=lambda m: (m["in_mtok"] is None, m["in_mtok"] or 0))

    # split into bands by count → take per_band from each band at even intervals (price spread).
    picked = []
    n = len(pool)
    if n:
        size = max(1, -(-n // bands))            # round up = count per band
        for b in range(0, n, size):
            grp = pool[b:b + size]
            if len(grp) <= per_band:
                picked += grp
            else:                                # within the band too, per_band at even intervals
                idx = sorted({round(i * (len(grp) - 1) / (per_band - 1))
                              for i in range(per_band)}) if per_band > 1 else [0]
                picked += [grp[j] for j in idx]

    # src first, dedup per base model (avoid double-probing different paths of the same model).
    chosen, seen_base, dropped_dup = [], set(), []
    for m in src + picked:
        base = _base_model(m["model_id"])
        if base in seen_base:
            dropped_dup.append(m["key"]); continue
        seen_base.add(base)
        chosen.append(m)
    chosen.sort(key=lambda m: (m["in_mtok"] is None, m["in_mtok"] or 0))
    priced = [m["in_mtok"] for m in chosen if m["in_mtok"] is not None]
    info = {
        "n_total": len(models), "n_pool": n, "n_chosen": len(chosen), "bands": bands,
        "n_src": len([m for m in chosen if m["origin"] == "src"]),
        "dropped_legacy": dropped_legacy, "dropped_dup": dropped_dup,
        "price_min": min(priced) if priced else None,
        "price_max": max(priced) if priced else None,
    }
    return [m["key"] for m in chosen], info


def _base_model(model_id):
    """Base model identifier with the data-residency path prefix (jp./apac./us./eu.) stripped."""
    parts = model_id.split(".", 1)
    if len(parts) == 2 and parts[0] in ("jp", "apac", "us", "eu"):
        return parts[1]
    return model_id


def _fmt_price(v):
    return "null" if v is None else f"{v:g}"   # YAML null (? is a reserved char and invalid)


def to_models_yaml(models, cascade=None):
    """Serialize the normalized candidate list to MODELS.yaml (gold format)."""
    src = [m for m in models if m["origin"] == "src"]
    bed = [m for m in models if m["origin"] == "bedrock"]
    other = [m for m in models if m["origin"] not in ("src", "bedrock")]

    L = [
        "# MODELS.yaml — the target's candidate model catalog (Nishiki area, canonical)",
        "#",
        "# auto-generated by nishiki init (src=from target code / bedrock=list_foundation_models live enumeration).",
        "# origin: src=wired-in current (always kept as baseline) / bedrock=catalog enumeration / openrouter=cross-border (needs residency approval).",
        "# price = USD / 1M tokens (in, out). price_src: A=vendor published / B=AWS price list (needs ap-ne confirm).",
        "# call: on_demand=modelId direct call / profile=via inference profile (jp./apac.=data residency).",
        "# quality_hint: known evaluation for Japanese company/bank name OCR (good/trap/unknown=unevaluated=confirm via probe).",
        "",
        "models:",
    ]
    if src:
        L.append("  # ---- origin=src: current models wired into the target code. Always kept as baseline ----")
        L += [_model_line(m) for m in src]
    if bed:
        L.append("  # ---- origin=bedrock: list_foundation_models live enumeration. quality_hint unevaluated=confirm via probe ----")
        L += [_model_line(m) for m in bed]
    if other:
        L += [_model_line(m) for m in other]

    known = [m["key"] for m in src]
    new = [m["key"] for m in bed]
    L.append("")
    L.append(f"# default run sets: ALL=all {len(models)} / "
             f"KNOWN=the {len(known)} with origin:src / NEW=the {len(new)} with origin:bedrock.")
    if cascade:
        L.append(f"# CASCADE is separate (the target's current *_CASCADE={list(cascade)}) = gold baseline.")
    return "\n".join(L) + "\n"


def _model_line(m):
    fields = (
        f"origin: {m['origin']}, model_id: {m['model_id']}, call: {m['call']}, "
        f"in: {_fmt_price(m['in_mtok'])}, out: {_fmt_price(m['out_mtok'])}, "
        f"price_src: {m['price_src'] or 'null'}, tier: {m['tier']}, quality_hint: {m['quality_hint']}"
    )
    if m.get("lifecycle") and m["lifecycle"] != "ACTIVE":
        fields += f", life: {m['lifecycle']}"     # LEGACY etc. are not probed (dropped in candidate selection)
    if m.get("note"):
        fields += f', note: "{m["note"]}"'
    return f"  {m['key']+':':18} {{ {fields} }}"


def to_providers_yaml(models):
    """Serialize the selected models into a providers.yaml snippet (OpenRouter=openai_compatible)."""
    lines = ["providers:"]
    for m in models:
        key = m["id"].replace("/", "_").replace(":", "_").replace(".", "_")
        lines += [
            f"  {key}:",
            "    kind: openai_compatible",
            f"    endpoint: {OPENROUTER_ENDPOINT}",
            "    api_key_env: OPENROUTER_API_KEY",
            f"    model: \"{m['id']}\"",
            f"    price: {{ in_per_1k: {m['in_per_1k']:.6g}, out_per_1k: {m['out_per_1k']:.6g}, unit: USD }}",
        ]
    return "\n".join(lines) + "\n"
