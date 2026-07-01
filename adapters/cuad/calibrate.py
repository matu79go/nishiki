"""Nishiki calibrate — CUAD (contract-clause extraction) adapter. **Non-classification KPI = extracted-span F1**.

Design doc §18.9 / 2026-06-22 verification bed for non-classification KPI generalization. By writing it in the
**same frame** (the `nishiki.runner` Adapter contract) as example_target (classification = verdict match), it
proves the generalization "add an adapter per target and you can measure it." The choke is one clear function =
`extract_clause` (ask the LLM to "extract this clause").

CUAD = Contract Understanding Atticus Dataset (OSS, CC BY 4.0). 510 contracts × 41 clause questions,
expert span annotations. SQuAD-format JSON (data[].paragraphs[].qas[], answers[].answer_start).

Differences from example_target (= the target-specific part the adapter absorbs):
  - load_items : reads CUAD JSON (a file, not a DB). gold = expert-annotated spans.
  - choke      : a thin self-built agent function (**our code**, not the target app).
  - scoring    : **span-overlap F1** (continuous scalar 0..1), not classification = has no ng_recall.
  - data residency : CUAD real data is kept only on your data host. CC-BY attribution observed.

Run (local, with nishiki pip-installed):
  NZ_MODE=probe NZ_PROBE_N=3 NZ_CANDIDATES=ALL NZ_DATA=/path/CUADv1.json \
    OPENROUTER_API_KEY=... python "$(nishiki adapter-path cuad)"
  NZ_MODE=run NZ_CANDIDATES=gpt-5-nano,qwen3-vl ... python "$(nishiki adapter-path cuad)" > run.json

Environment variables:
  NZ_MODE       probe / run.
  NZ_DATA       path to the CUAD SQuAD-format JSON (on the data host).
  NZ_CANDIDATES ALL / NEW / KNOWN / comma-separated keys.
  NZ_CATALOG    profile-driven catalog (JSON), produced by calibrate-env. Falls back to CATALOG below if absent.
  NZ_PROBE_N    number of items run per candidate in probe (default 3).
  NZ_LIMIT      cap on items for run/probe (0 = all).
  NZ_OPENAI_BASE / OPENROUTER_API_KEY  OpenAI-compatible endpoint (default OpenRouter).
"""
import base64  # noqa: F401  (reserved for future vision contract scanning)
import json
import os
import sys
import time
import urllib.error
import urllib.request

# Import the shared runner/scoring. The local run (nishiki editable install) is the main path. Add a
# fallback to src-layout so it still works when not installed (during tests/dev). No container piping.
try:
    from nishiki import runner, scoring
except ImportError:  # pragma: no cover - dev fallback
    _SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src")
    sys.path.insert(0, _SRC)
    from nishiki import runner, scoring


# ───────────────────────── candidate catalog (text models, via OpenRouter) ──────────────
# key: {"model_id","in","out","note"}. in/out = USD/1M tok. CUAD is pure text = no vision needed.
# If NZ_CATALOG (produced by calibrate-env) is present it overrides/extends this (profile-driven).
CATALOG = {
    "gpt-5-nano":    {"model_id": "openai/gpt-5-nano",          "in": 0.05, "out": 0.40, "note": "cheap", "call": "openai_compatible"},
    "qwen3-vl":      {"model_id": "qwen/qwen3-vl-235b-a22b-instruct", "in": 0.20, "out": 0.88, "note": "production-floor equivalent", "call": "openai_compatible"},
    "llama-4-scout": {"model_id": "meta-llama/llama-4-scout",   "in": 0.10, "out": 0.30, "note": "open", "call": "openai_compatible"},
}
KNOWN = ["qwen3-vl"]   # treated as baseline = on par with the example_target production floor

_inj = os.environ.get("NZ_CATALOG")
if _inj:
    try:
        for k, v in json.loads(_inj).items():
            CATALOG[k] = {"model_id": v["model_id"], "in": float(v["in"]),
                          "out": float(v["out"]), "note": v.get("note", ""),
                          "call": v.get("call", "openai_compatible")}
    except (ValueError, KeyError, TypeError) as e:
        print(f"[warn] failed to parse NZ_CATALOG ({e}); continuing with default CATALOG", file=sys.stderr)


# ───────────────────────── CUAD parse + span location (pure functions) ──────────────────────
def parse_cuad(squad, limit=0, balance=False):
    """CUAD (SQuAD-format dict) → items for the runner (pure function, no network).

    Each (contract × clause question) becomes one item. gold = expert-annotated character spans [(start,end), ...]
    (is_impossible / empty answers = "no such clause" = empty gold spans).

    balance=True: CUAD is ~68% is_impossible (not applicable). Measured as-is, a do-nothing that "always says NONE"
    can score F1≈0.68 and pollute the KPI. Keep only as many impossible as answerable and interleave them =
    corrects the do-nothing baseline to ≈0.5 (interleave so the probe is not skewed).
    Returns: [{"id","question","context","gold":[(s,e),...]}]
    """
    answerable, impossible = [], []
    for doc in squad.get("data", []):
        title = doc.get("title", "doc")
        for para in doc.get("paragraphs", []):
            context = para.get("context", "")
            for qa in para.get("qas", []):
                gold = []
                if not qa.get("is_impossible"):
                    for a in qa.get("answers", []):
                        s = a.get("answer_start")
                        t = a.get("text", "")
                        if isinstance(s, int) and t:
                            gold.append((s, s + len(t)))
                it = {"id": f"{title}::{qa.get('id', len(answerable) + len(impossible))}",
                      "question": qa.get("question", ""), "context": context, "gold": gold}
                (answerable if gold else impossible).append(it)

    if balance:
        impossible = impossible[:len(answerable)]      # trim to equal count (answerable is the reference amount)
        items = []
        for i in range(max(len(answerable), len(impossible))):  # interleave (keep the probe representative)
            if i < len(answerable):
                items.append(answerable[i])
            if i < len(impossible):
                items.append(impossible[i])
    else:
        items = answerable + impossible                # original coverage (answerable → not)

    return items[:limit] if limit else items


def locate_spans(context, answer_text):
    """Extracted text (model output) → character spans [(start,end), ...] within context (pure function).

    Assumes the model returns "verbatim quotes of the relevant part" separated by lines/delimiters. Find each
    fragment in context and recover its position (first occurrence). "NONE"/empty = not applicable (empty spans).
    Fragments not found are dropped (= hallucinated quotes do not count in scoring = correctly penalized).
    """
    if not answer_text:
        return []
    spans = []
    for line in answer_text.replace("\r", "\n").split("\n"):
        # Strip bullet markers first, then quotes (order matters: turn '- "x"' into 'x')
        frag = line.strip().strip("-•*  ").strip().strip('"').strip("'").strip()
        if not frag or frag.upper() == "NONE" or len(frag) < 4:
            continue
        idx = context.find(frag)
        if idx >= 0:
            spans.append((idx, idx + len(frag)))
    return spans


_EXTRACT_PROMPT = (
    "You are a contract-analysis assistant. Below is a contract and a question about a "
    "specific clause. Extract the EXACT verbatim text span(s) from the contract that answer "
    "the question — copy substrings character-for-character, one per line. Output ONLY the "
    "extracted substrings with no commentary, numbering, or quotes. If the contract contains "
    "no such clause, output exactly: NONE\n\n"
    "# Question\n{question}\n\n# Contract\n{context}\n\n# Extracted span(s)\n"
)


# ───────────────────────── choke: clause extraction via LLM (single call) ──────────────────────────
_OPENAI_BASE = os.environ.get("NZ_OPENAI_BASE", "https://openrouter.ai/api/v1").rstrip("/")
_OPENAI_URL = _OPENAI_BASE + "/chat/completions"


def openai_chat(model_id, prompt, max_tokens=1024):
    """Call OpenAI-compatible chat/completions once (text only). Returns the same-shaped dict as Bedrock converse."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set (CUAD runs via OpenRouter)")
    body = json.dumps({"model": model_id, "max_tokens": max_tokens,
                       "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
    req = urllib.request.Request(
        _OPENAI_URL, data=body, method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                 "HTTP-Referer": "https://github.com/matu79go/nishiki",
                 "X-Title": "nishiki-cuad"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"OpenRouter {e.code}: {detail}") from e
    msg = (resp.get("choices") or [{}])[0].get("message", {}) or {}
    text = msg.get("content") or ""
    if isinstance(text, list):
        text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
    usage = resp.get("usage", {}) or {}
    return {"text": text, "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0)}


# ───────────────────────── choke (alternate path): Bedrock converse (boto3) ──────────────────────
# bedrock candidates (call=on_demand/profile) go here. AWS auth via env (AWS_ACCESS_KEY_ID etc.).
# The data host has no ~/.aws, so pass example_target/.env's AWS_* as env vars (your own cloud, not cross-border).
_BEDROCK_CLIENT = None


def _bedrock_client():
    global _BEDROCK_CLIENT
    if _BEDROCK_CLIENT is None:
        import boto3  # lazy import (boto3 not needed when using openrouter only)
        _BEDROCK_CLIENT = boto3.client(
            "bedrock-runtime", region_name=os.environ.get("AWS_REGION", "ap-northeast-1"))
    return _BEDROCK_CLIENT


def bedrock_chat(model_id, prompt, max_tokens=1024):
    """Call Bedrock converse once (text only). Returns the same-shaped dict as openai_chat."""
    r = _bedrock_client().converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": max_tokens})
    blocks = r.get("output", {}).get("message", {}).get("content", []) or []
    text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
    u = r.get("usage", {}) or {}
    return {"text": text, "input_tokens": u.get("inputTokens", 0),
            "output_tokens": u.get("outputTokens", 0)}


def _call_for(kind):
    """Candidate call type → choke function. openai_compatible=OpenRouter / on_demand|profile=Bedrock."""
    return bedrock_chat if kind in ("on_demand", "profile") else openai_chat


def extract_clause(context, question, model_id, *, call=openai_chat, max_tokens=1024):
    """CHOKE (the heart of generalization): have the LLM extract the clause and return (text, in_tok, out_tok).

    Swap call= to point at any backend / test fake (same idea as example_target's model_call_module swap).
    """
    prompt = _EXTRACT_PROMPT.format(question=question, context=context)
    r = call(model_id, prompt, max_tokens=max_tokens)
    return r["text"], r.get("input_tokens", 0), r.get("output_tokens", 0)


# ───────────────────────── Adapter (satisfies the runner contract) ─────────────────────────────
class CuadAdapter:
    """runner.Adapter contract. labels=None (non-classification) = no ng_*; floors is kpi_floor only."""
    labels = None
    positive = None
    reference = None

    def __init__(self, items=None, catalog=None, known=None, call=None):
        self.catalog = catalog if catalog is not None else CATALOG
        self.known = known if known is not None else KNOWN
        self._call_override = call          # tests inject a fake (None = auto-dispatch by the catalog's call)
        self._call = openai_chat
        self._items = items if items is not None else self._load_from_env()
        self._model_id = None

    def _load_from_env(self):
        path = os.environ.get("NZ_DATA")
        if not path:
            raise RuntimeError("NZ_DATA not set (path to the CUAD SQuAD JSON, on the data host)")
        with open(path, encoding="utf-8") as f:
            squad = json.load(f)
        balance = os.environ.get("NZ_BALANCE", "1") not in ("0", "", "false", "False")
        return parse_cuad(squad, limit=int(os.environ.get("NZ_LIMIT", "0")), balance=balance)

    def load_items(self):
        return self._items

    def set_model(self, cand):
        e = self.catalog[cand]
        self._model_id = e["model_id"]
        self._price = (e["in"], e["out"])
        # Dispatch the choke by call (openai_compatible=OpenRouter / on_demand|profile=Bedrock).
        self._call = self._call_override or _call_for(e.get("call", "openai_compatible"))

    def run_item(self, item):
        t0 = time.perf_counter()
        text, in_tok, out_tok = extract_clause(item["context"], item["question"],
                                               self._model_id, call=self._call)
        latency = time.perf_counter() - t0
        spans = locate_spans(item["context"], text)
        pin, pout = self._price
        cost = in_tok / 1e6 * pin + out_tok / 1e6 * pout
        return spans, cost, latency, None

    def score_item(self, pred, gold):
        return scoring.span_overlap_f1(pred, gold)


def main():
    mode = os.environ.get("NZ_MODE", "probe")
    adapter = CuadAdapter()
    cands = runner.resolve_candidates(os.environ.get("NZ_CANDIDATES", "ALL"),
                                      adapter.catalog, adapter.known)
    if not cands:
        print("no candidates (NZ_CANDIDATES)", file=sys.stderr)
        sys.exit(1)
    if mode == "probe":
        out = runner.mode_probe(adapter, cands, int(os.environ.get("NZ_PROBE_N", "3")))
    elif mode == "run":
        out = runner.mode_run(adapter, cands)
    else:
        print(f"unknown NZ_MODE={mode} (probe|run)", file=sys.stderr)
        sys.exit(1)
    runner.emit(out)


if __name__ == "__main__":
    main()
