"""Generic adapter — measure **without hand-writing per-target glue**, driven by KOI.yaml (config) (core of the release, design doc §18.9).

A shared runtime for measuring "the user's KPI, classification or not". It assembles the common shape
  = dataset (gold loader) × prompt template × response parser × scorer name × model backend
from config and satisfies the `runner.Adapter` contract. Once init reads the source and authors the
config, a new target can be KOI-measured with no hand-written adapter.

  Note: special shapes like example_target (target DB + container + module-dict swap) keep a thin
    dedicated glue but use the shared scoring/runner (out of scope for generalization = bespoke
    coupling that config can't express).

Config keys (config dict, from KOI.yaml):
  task_type   : "classification" | "extraction" (used for the scorer's default mapping)
  kpi         : scoring registry name (defaults from task_type if omitted). e.g. "span_f1" / "label_match"
  gold_format : loader name. e.g. "squad"
  gold_data   : path to the dataset
  prompt      : prompt template (each item field expanded via {field})
  parser      : response parser name. e.g. "locate_spans" (extraction) / "identity" (as-is) / "label" (classification)
  labels/positive : extra dimensions for classification tasks (NG recall etc.). Not needed for non-classification
  reference   : the baseline candidate for current-model comparison (if any)
"""
import time

from . import backends, scoring


# ───────────────────────── gold loaders (dataset → runner items) ───────────────
def load_squad(path, limit=0, balance=False):
    """SQuAD-format JSON (CUAD etc.) → items [{id, question, context, gold:[(s,e)]}].

    is_impossible / empty answers = none applicable (empty gold). balance=True trims impossible to
    the same count as answerable and interleaves them (corrects the do-nothing trap of a
    none-applicable majority).
    """
    import json
    with open(path, encoding="utf-8") as f:
        squad = json.load(f)
    answerable, impossible = [], []
    for doc in squad.get("data", []):
        title = doc.get("title", "doc")
        for para in doc.get("paragraphs", []):
            context = para.get("context", "")
            for qa in para.get("qas", []):
                gold = []
                if not qa.get("is_impossible"):
                    for a in qa.get("answers", []):
                        s, t = a.get("answer_start"), a.get("text", "")
                        if isinstance(s, int) and t:
                            gold.append((s, s + len(t)))
                it = {"id": f"{title}::{qa.get('id', len(answerable) + len(impossible))}",
                      "question": qa.get("question", ""), "context": context, "gold": gold}
                (answerable if gold else impossible).append(it)
    if balance:
        impossible = impossible[:len(answerable)]
        items = []
        for i in range(max(len(answerable), len(impossible))):
            if i < len(answerable):
                items.append(answerable[i])
            if i < len(impossible):
                items.append(impossible[i])
    else:
        items = answerable + impossible
    return items[:limit] if limit else items


def load_jsonl(path, limit=0, balance=False):
    """JSONL (one item per line: {id?, gold, ...arbitrary fields}) → items. The plain shape for classification/generic."""
    import json
    items = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            o = json.loads(line)
            o.setdefault("id", str(i))
            items.append(o)
            if limit and len(items) >= limit:
                break
    return items


def _sqlite_schema(db_path):
    """Read the DB's CREATE TABLE statements (schema text the model needs to write correct SQL)."""
    import sqlite3
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL ORDER BY name"
        ).fetchall()
    finally:
        conn.close()
    return "\n".join(r[0] for r in rows)


def _run_select(db_path, sql):
    """Execute a read-only SELECT against the SQLite DB and return rows as lists (raises on any write)."""
    import sqlite3
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        return [list(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def load_sql(path, limit=0, balance=False):
    """Text-to-SQL gold (JSONL of {question, gold_sql, db?}) → items with the DB schema + expected rows.

    The schema is introspected from the SQLite DB (no need to hand-copy CREATE statements into gold),
    and each item's gold is the **result set of gold_sql** (computed once here, so scoring only has to
    run the model's SQL and compare). `db` defaults to shop.db next to the gold file. The DB is opened
    read-only everywhere. `_db` (absolute path, used by parse_sql at run time) is carried on the item but
    never written to the run JSON (per-item output keeps only id/gold/pred/score/cost).
    """
    import json
    import os
    base = os.path.dirname(os.path.abspath(path))
    schema_cache = {}
    items = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            o = json.loads(line)
            db_abs = os.path.join(base, o.get("db", "shop.db"))
            if db_abs not in schema_cache:
                schema_cache[db_abs] = _sqlite_schema(db_abs)
            items.append({
                "id": o.get("id", str(i)),
                "question": o.get("question", ""),
                "schema": schema_cache[db_abs],
                "_db": db_abs,
                "gold": _run_select(db_abs, o["gold_sql"]),
            })
            if limit and len(items) >= limit:
                break
    return items


LOADERS = {"squad": load_squad, "jsonl": load_jsonl, "sql": load_sql}


# ───────────────────────── response parsers (LLM response → scorable pred) ──────────────
def _normalize_ws(s):
    """Return a string with runs of whitespace collapsed to one, plus a map from normalized index → original index.

    Contract text is full of newlines/runs of spaces, so whitespace differs between model output and
    the source and exact match tends to fail. Normalize before matching, and build a map to convert
    hit positions back to the original offsets.
    """
    norm, omap, prev_space = [], [], False
    for i, ch in enumerate(s):
        if ch.isspace():
            if prev_space:
                continue
            norm.append(" "); omap.append(i); prev_space = True
        else:
            norm.append(ch); omap.append(i); prev_space = False
    omap.append(len(s))                       # trailing sentinel (for end)
    return "".join(norm), omap


def _find_span(context, frag):
    """Find frag in context and return (start, end) (None if absent).

    Stages: (1) exact → (2) whitespace-normalized (case-sensitive) → (3) whitespace-normalized + case-insensitive.
    Preserves the essence of verbatim extraction (paraphrase is not allowed) while absorbing only
    whitespace/newline/case variation.
    """
    idx = context.find(frag)
    if idx >= 0:
        return idx, idx + len(frag)
    nctx, omap = _normalize_ws(context)
    nfrag, _ = _normalize_ws(frag)
    nfrag = nfrag.strip()
    if len(nfrag) < 4:
        return None
    for hay, needle in ((nctx, nfrag), (nctx.lower(), nfrag.lower())):
        j = hay.find(needle)
        if j >= 0:
            return omap[j], omap[min(j + len(needle), len(omap) - 1)]
    return None


def parse_locate_spans(item, text):
    """Extraction: find the verbatim fragments returned by the LLM within context and map to character spans [(s,e)] (absorbing variation)."""
    context = item.get("context", "")
    if not text:
        return []
    spans = []
    for line in text.replace("\r", "\n").split("\n"):
        frag = line.strip().strip("-•*  ").strip().strip('"').strip("'").strip()
        if not frag or frag.upper() == "NONE" or len(frag) < 4:
            continue
        sp = _find_span(context, frag)
        if sp:
            spans.append(sp)
    return spans


def parse_identity(item, text):
    """As-is (strip surrounding whitespace) = pred. The naive shape for generation/free text."""
    return (text or "").strip()


def parse_label(item, text, labels=None):
    """Classification: normalize the response to one of the label set (exact match or containment). Raw text if not found."""
    t = (text or "").strip()
    for lab in (labels or []):
        if t == lab or lab in t:
            return lab
    return t


def _extract_sql(text):
    """Pull the SQL statement out of a model response (strip ```sql fences / leading prose)."""
    if not text:
        return ""
    t = text.strip()
    if "```" in t:                                  # fenced block: take the first fenced body
        parts = t.split("```")
        if len(parts) >= 2:
            body = parts[1]
            if body[:3].lower() == "sql":
                body = body[3:]
            t = body.strip()
    low = t.lower()                                 # else: start at the first SELECT/WITH keyword
    idx = min((low.find(k) for k in ("select", "with") if low.find(k) >= 0), default=-1)
    if idx > 0:
        t = t[idx:]
    return t.split(";")[0].strip()                  # one statement, no trailing semicolon


def parse_sql(item, text):
    """Text-to-SQL: extract the model's SQL, run it read-only against the item's DB, return {sql, rows|error}.

    Executing here (not in the scorer) keeps the scorer a pure result-set comparison and lets the DB
    handle come off the item (`_db`), so nothing target-specific leaks into scoring or the run JSON.
    """
    sql = _extract_sql(text)
    db = item.get("_db")
    out = {"sql": sql}
    if not sql:
        out["error"] = "no SQL found in response"
        return out
    if db is None:
        out["error"] = "no DB bound to item"
        return out
    try:
        out["rows"] = _run_select(db, sql)
    except Exception as e:                          # noqa: BLE001 — any SQL error = a failed (0.0) item
        out["error"] = str(e)[:200]
    return out


PARSERS = {"locate_spans": parse_locate_spans, "identity": parse_identity,
           "label": parse_label, "sql": parse_sql}


# ───────────────────────── generic adapter ─────────────────────────────────────────────
class GenericAdapter:
    """Satisfies the runner.Adapter contract from KOI.yaml config. No per-target hand-writing (common shape only)."""

    def __init__(self, config, catalog, *, items=None, call=None, limit=0):
        self.catalog = catalog
        self.known = config.get("known", [])
        self.reference = config.get("reference")
        tt = config.get("task_type", "extraction")
        self.labels = config.get("labels")          # classification only (None = non-classification = no ng_*)
        self.positive = config.get("positive")
        self._scorer = scoring.get_scorer(config.get("kpi") or scoring.TASK_SCORER[tt])
        self._prompt = config["prompt"]
        parser = PARSERS[config.get("parser", "identity")]
        if config.get("parser") == "label":         # the label parser binds labels
            labels = self.labels or []
            self._parser = lambda it, tx: parse_label(it, tx, labels)
        else:
            self._parser = parser
        self._call_override = call
        self._call = backends.openai_chat
        self._model_id = None
        self._price = (0.0, 0.0)
        if items is not None:
            self._items = items
        else:
            loader = LOADERS[config["gold_format"]]
            self._items = loader(config["gold_data"], limit=limit,
                                 balance=config.get("balance", False))

    @classmethod
    def from_koi(cls, koi, catalog, *, items=None, call=None, limit=0, gold_data=None):
        """Build a GenericAdapter from a parsed KOI.yaml (dict) = run the auto-generated config directly.

        Maps the KOI.yaml authored by init (task_type / scorer.kpi / gold / run.{prompt,parser,gold_format} /
        candidates / reference) straight into run config. No target-specific glue (core of the release).
        """
        run = koi.get("run") or {}
        gold = koi.get("gold") or {}
        config = {
            "task_type": koi.get("task_type", "extraction"),
            "kpi": (koi.get("scorer") or {}).get("kpi"),
            "gold_format": run.get("gold_format", gold.get("source") if gold.get("source") in LOADERS else "jsonl"),
            "gold_data": gold_data or gold.get("data"),
            "prompt": run.get("prompt", "{text}"),
            "parser": run.get("parser", "identity"),
            "balance": run.get("balance", False),
            "labels": koi.get("labels"), "positive": koi.get("positive"),
            "reference": koi.get("reference"),
            "known": koi.get("known", []),
        }
        return cls(config, catalog, items=items, call=call, limit=limit)

    def load_items(self):
        return self._items

    def set_model(self, cand):
        e = self.catalog[cand]
        self._model_id = e["model_id"]
        self._price = (e["in"], e["out"])
        self._call = self._call_override or backends.backend_for(e.get("call", "openai_compatible"))

    def run_item(self, item):
        fields = {k: v for k, v in item.items() if k != "gold"}
        # Absorb prompt field-name variation (resolve to context even if the AI wrote {contract_text} etc.).
        if "context" in fields:
            for alias in ("contract_text", "contract", "document", "text", "passage"):
                fields.setdefault(alias, fields["context"])
        try:
            prompt = self._prompt.format(**fields)
        except KeyError as e:                  # an unknown placeholder raises explicitly (catch config mistakes early)
            raise RuntimeError(f"prompt's {{{e.args[0]}}} is not a data field"
                               f" (available: {sorted(fields)})") from None
        t0 = time.perf_counter()
        r = self._call(self._model_id, prompt)
        latency = time.perf_counter() - t0
        pred = self._parser(item, r["text"])
        cost = (r.get("input_tokens", 0) / 1e6 * self._price[0]
                + r.get("output_tokens", 0) / 1e6 * self._price[1])
        return pred, cost, latency, None

    def score_item(self, pred, gold):
        return self._scorer(pred, gold)
