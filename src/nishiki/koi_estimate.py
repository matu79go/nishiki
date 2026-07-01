"""Edge KOI estimator — an instant KOI estimate for a runtime prompt WITHOUT calling any model.

This is the core of the "evolving KOI" idea: sit beside the agent and, for the current prompt / input
and a chosen model, show KOI right away — cost computed locally (token count × MODELS.yaml price), KPI
reused from the last measured run (per model). KOI = KPI ÷ cost (the profile's `koi.formula`).

- For an unchanged prompt size it reproduces the measured KOI; change the prompt or the model/route and
  it updates instantly, with zero model calls and zero spend.
- It improves as more real runs accumulate (the measured KPI / cost basis is refreshed each run).

v1 token estimates are deliberately simple (text ≈ chars/4; image ≈ area/750 from the file's pixel
dimensions). Override with explicit --in-tokens / --out-tokens, or refine the heuristics later (v2).
"""
import os
import struct

__all__ = ["text_tokens", "image_dims", "image_dims_bytes", "image_tokens",
           "load_basis", "resolve_model", "estimate"]

_IMG_TOKENS_PER_PX = 1 / 750.0   # rough vision-token approximation (area-based); refine in v2
_DEFAULT_OUT_TOKENS = 256        # fallback when output length is unknown and not measured


def text_tokens(s):
    """Approximate token count of text (≈ chars/4). Cheap, model-free; refine with a real tokenizer in v2."""
    return max(1, (len(s or "") + 3) // 4)


def _dims_from_stream(f):
    """(width, height) of a PNG/JPEG from a binary file-like (seekable). None if undeterminable."""
    head = f.read(26)
    if head[:8] == b"\x89PNG\r\n\x1a\n":                        # PNG: IHDR width/height at 16..24
        return struct.unpack(">II", head[16:24])
    if head[:2] == b"\xff\xd8":                                 # JPEG: scan SOF markers
        f.seek(2)
        while True:
            b = f.read(1)
            if not b:
                return None
            if b != b"\xff":
                continue
            marker = f.read(1)
            while marker == b"\xff":                            # skip fill bytes
                marker = f.read(1)
            m = marker[0] if marker else 0
            if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
                f.read(3)                                        # length(2) + precision(1)
                h, w = struct.unpack(">HH", f.read(4))
                return (w, h)
            seg = f.read(2)
            if len(seg) < 2:
                return None
            f.seek(struct.unpack(">H", seg)[0] - 2, os.SEEK_CUR)
    return None


def image_dims(path):
    """(width, height) of a PNG/JPEG file by parsing the header only (no Pillow). None if undeterminable."""
    try:
        with open(path, "rb") as f:
            return _dims_from_stream(f)
    except (OSError, struct.error):
        return None


def image_dims_bytes(data):
    """(width, height) of a PNG/JPEG held in memory (e.g. a base64-decoded data URL). None if undeterminable."""
    import io
    try:
        return _dims_from_stream(io.BytesIO(data))
    except struct.error:
        return None


def image_tokens(width, height):
    """Approximate vision input tokens for an image of the given pixel dimensions (area-based)."""
    return max(1, round(width * height * _IMG_TOKENS_PER_PX))


def per_item_list(candidate):
    """A scored-run candidate's per-item detail list, found by SHAPE (not by key name).

    The key varies per target (`per_item`, `per_doc`, …), so we pick the longest value that is a list of
    dicts carrying a 'cost'. Target-agnostic; returns the list, or None if the run kept no per-item detail.
    Shared by the live dashboard (charts) and koi_report (false-alarm rate) so neither hardcodes a name.
    """
    best = None
    if isinstance(candidate, dict):
        for v in candidate.values():
            if (isinstance(v, list) and v and all(isinstance(x, dict) for x in v) and "cost" in v[0]
                    and (best is None or len(v) > len(best))):
                best = v
    return best


def load_basis(experiment):
    """Read the profile's measurement basis: per-model measured KPI + cost, prices, kpi name, reference.

    Returns {kpi_name, reference, run_path, measured: {model: {kpi, cost_per_item}}, prices: {model: (in, out)}}.
    """
    import yaml
    koi_path = os.path.join(experiment, "KOI.yaml")
    models_path = os.path.join(experiment, "MODELS.yaml")
    koi = yaml.safe_load(open(koi_path, encoding="utf-8")) if os.path.exists(koi_path) else {}
    models = (yaml.safe_load(open(models_path, encoding="utf-8")) or {}).get("models", {}) \
        if os.path.exists(models_path) else {}
    kpi_name = (koi.get("scorer") or {}).get("kpi")
    reference = koi.get("reference")

    runs = os.path.join(experiment, "runs")                       # newest scored run = the KPI/cost basis
    cand = sorted((os.path.join(runs, f) for f in os.listdir(runs) if f.endswith("_run.json"))
                  if os.path.isdir(runs) else [],
                  key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0)
    run_path = cand[-1] if cand else None

    measured = {}
    if run_path:
        import json
        run = json.load(open(run_path, encoding="utf-8"))
        for c in run.get("candidates", []):
            label = c.get("label") or c.get("key")
            if not label:
                continue
            # KPI for KOI is a RATE in [0,1]. If the scorer names a count field (a known mistake:
            # scorer.kpi="overall_agree" = raw agreement count), it would be >1 — fall back to the
            # canonical rate slot (overall_acc / kpi) so live KOI matches koi_report's HTML.
            kpi = c.get(kpi_name) if kpi_name else None
            if kpi is None or (isinstance(kpi, (int, float)) and kpi > 1):
                kpi = c.get("overall_acc")
                if kpi is None:
                    kpi = c.get("kpi")
            measured[label] = {"kpi": kpi, "cost_per_item": c.get("cost_per_item"),
                               "latency_ms_p50": c.get("latency_ms_p50"),    # separate axis (not in KOI)
                               "latency_ms_p95": c.get("latency_ms_p95")}
    prices = {k: (m.get("in"), m.get("out")) for k, m in models.items()}
    id_to_key = {m.get("model_id"): k for k, m in models.items() if m.get("model_id")}
    return {"kpi_name": kpi_name, "reference": reference, "run_path": run_path,
            "measured": measured, "prices": prices, "id_to_key": id_to_key}


def resolve_model(basis, model):
    """Resolve a raw upstream model_id (e.g. from a probe event) to the profile candidate key."""
    if model is None:
        return None
    return (basis.get("id_to_key") or {}).get(model, model)


def estimate(experiment, *, model=None, prompt=None, image=None,
             in_tokens=None, out_tokens=None, basis=None):
    """Estimate KOI for `model` (or every measured model) on the given prompt/image — no model call.

    Cost: if any of in_tokens / prompt / image is given, cost is computed from those tokens × price;
    otherwise the last run's measured cost_per_item is reused (so KOI reproduces the measured value).
    KPI: reused from the last measured run for that model. Returns {kpi_name, reference, run_path, rows}.
    """
    basis = basis or load_basis(experiment)
    measured, prices = basis["measured"], basis["prices"]
    model = resolve_model(basis, model)                         # accept a raw model_id too
    targets = [model] if model else list(measured) or list(prices)

    have_prompt = in_tokens is not None or prompt is not None or image is not None
    img_tok = img_dims = None
    if image is not None:
        img_dims = image_dims(image)
        img_tok = image_tokens(*img_dims) if img_dims else None

    rows = []
    for m in targets:
        meas = measured.get(m, {})
        kpi = meas.get("kpi")
        in_p, out_p = prices.get(m, (None, None))
        it = ot = None
        if have_prompt:
            it = (in_tokens or 0) + (text_tokens(prompt) if prompt is not None else 0) + (img_tok or 0)
            ot = out_tokens if out_tokens is not None else _DEFAULT_OUT_TOKENS
            if in_p is not None:
                cost = it / 1e6 * in_p + ot / 1e6 * out_p
                cost_src = "from prompt"
            else:                                  # unpriced (e.g. a composite reference like CASCADE)
                cost = meas.get("cost_per_item")   # → reuse its measured cost so the vs-reference ratio works
                cost_src = "measured (unpriced)"
                it = ot = None
        else:
            cost = meas.get("cost_per_item")
            cost_src = "measured cost reused"
        koi = (kpi / cost) if (kpi is not None and cost) else None
        rows.append({"model": m, "kpi": kpi, "cost_per_item": cost, "koi": koi,
                     "in_tokens": it, "out_tokens": ot, "cost_src": cost_src,
                     "priced": in_p is not None})

    # vs-reference ratio (1.0x = the reference/current route), using each row's estimated KOI
    ref = basis["reference"]
    ref_koi = next((r["koi"] for r in rows if r["model"] == ref), None)
    if ref_koi:
        for r in rows:
            r["vs_reference"] = (r["koi"] / ref_koi) if r["koi"] else None
    return {"kpi_name": basis["kpi_name"], "reference": ref, "run_path": basis["run_path"],
            "image_dims": img_dims, "image_tokens": img_tok, "rows": rows}
