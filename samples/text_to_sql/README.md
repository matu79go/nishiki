# Sample: text-to-SQL — quality per dollar for a self-service-analytics agent

A tiny, self-contained example that shows **why KOI matters for a real production agent**, not just for
a one-off prompt. The task is text-to-SQL: turn a plain-English business question into a SQL query, run
it, and return the answer — the kind of self-service-analytics agent a team runs **thousands of times a
month**. At that volume the model you pick decides both your inference bill *and* how often a human has
to step in to fix a wrong answer. Nishiki measures the trade-off directly.

## Why this is a business problem, not a prompting demo

A person typing one question into a chat UI doesn't care whether the model costs \$0.02 or \$0.0002 —
it's one call. A *business* wires the same agent into a pipeline and runs it at scale. There, two numbers
dominate:

- **Inference cost per call** — at 5,000 questions/month, a frontier model at \$0.018/call is \$90/mo;
  a cheap model at \$0.0002/call is \$1/mo. Same job, **~100× the bill.**
- **Answer-correctness (pass rate)** — every wrong query is a wrong number in a report, which someone
  has to catch and redo. Lifting pass rate 72% → 88% turns ~1,400 human-follow-ups/month into ~600.

KOI = **pass rate ÷ cost** captures exactly this: the most *quality per dollar*, subject to a quality
floor (below the floor a model is disqualified, so a dirt-cheap model that returns garbage can't "win").

## 1. The task (what this agent does)

`agent.py` is a small AI agent. Given a **database schema** and a **question**, it asks a model to write
one SQLite `SELECT` that answers it.

```
Schema:   products(id, name, category, price), orders(...), customers(...), regions(...)
Question: "How many products cost 500 dollars or more?"
SQL:      SELECT COUNT(*) FROM products WHERE price >= 500     ← runs → returns 2 ✓
```

The agent's model call lives in one function, `generate_sql` — that's the **choke point** Nishiki
splices candidate models into (in memory; the source file is never edited).

## 2. The KPI — execution accuracy, graded automatically

We don't grade the *text* of the SQL (there are many correct phrasings). We **run** each generated query
read-only against `shop.db` and check whether its **result set equals the gold answer's** — real
execution accuracy: *did the generated code actually return the right rows?* Pass rate = fraction of the
14 questions answered correctly.

The bundled `.nishiki/KOI.yaml` encodes exactly that recipe — `task_type: sql` + `scorer.kpi:
sql_result_match`, with a **quality floor** of `0.8` (a model must answer ≥80% correctly to qualify).
The SQL runs on a **read-only** connection, so scoring can never modify the database.

## 3. The result — see it on the web

```bash
cd samples/text_to_sql
nishiki koi-report --web        # opens the KOI dashboard — no key, no model calls
```

This makes **zero model calls** and replays the bundled results in `.nishiki/`. Two tabs:

- **`/#measured`** — the leaderboard below, from `.nishiki/runs/*.json`.
- **`/#live`** — per-call KOI as the agent runs, from `.nishiki/live.jsonl`.

### Reading the table

`KOI = pass rate ÷ cost` = quality per dollar. First the floor (an 80% SLA) disqualifies anything below
80% pass rate; among the survivors, Nishiki ranks by KOI.

These are the **measured** numbers from the bundled run (14 *hard* analytics questions — window
functions, correlated subqueries, anti-joins, HAVING — on OpenRouter, ~\$1.2 total).

| model (route) | pass rate | \$ / item | KOI | verdict |
|---|---:|---:|---:|---|
| llama3b (`meta-llama/llama-3.2-3b-instruct`) | 0.07 (1/14) | \$0.00003 | ~~2551~~ | ✗ **cut by floor** — cheapest of all, but 7% correct |
| **deepseek** (`deepseek/deepseek-v4-pro`) | 0.93 (13/14) | \$0.00044 | **2135** | ★ **best value** — clears the bar, dirt cheap |
| glm46 (`z-ai/glm-4.6`) | 0.57 (8/14) | ~~\$0.00196~~ | ~~292~~ | ✗ cut by floor — below the SLA |
| gpt4 (`openai/gpt-4`) | 0.86 (12/14) | \$0.00969 | 88 | passes, mid-priced |
| gpt54pro (`openai/gpt-5.4-pro`) | 1.00 (14/14) | \$0.07047 | 14 | frontier — perfect, worst value |

**The takeaway — this is why KOI isn't just "pick the cheapest":**

- **The cheapest model has the highest *raw* KOI (2551) and is useless.** `llama3b` is nearly free, so
  dividing even a 7% pass rate by a tiny cost gives a huge KOI — it would top a naive ranking. The
  **quality floor is the guard**: at 7% correct it's disqualified, and so is `glm46` at 57%. Without the
  floor, "quality per dollar" would happily recommend a model that's wrong 93% of the time.
- **Among the models that clear the 80% bar, `deepseek` wins — and it is *not* the cheapest** (`llama3b`
  is). It wins because its quality-per-dollar is best: 93% correct at \$0.00044, a KOI of 2135.
- **The frontier `gpt54pro` has the highest pass rate (100%) and the *worst* KOI (14).** Its ~160×-higher
  cost bought just +7 points over deepseek (93→100). Paying that at scale is the expensive mistake KOI
  makes visible.

So KOI is neither "cheapest" (that's `llama3b`, disqualified) nor "highest quality" (that's the frontier,
terrible value) — it's the **best quality *per dollar* above your quality bar**. Latency is a separate
axis (not in KOI): gpt4 was fastest here (~2.4s), the frontier slowest (~41s).

### The optimization loop (raising KOI, not just measuring it)

Measuring picks the best model at today's prompt. You raise KOI further by **improving the prompt** at
the same model/cost: add the schema (already in the prompt here), add a couple of worked examples, or
tighten the instruction, then re-measure. Pass rate climbs while \$/item is unchanged, so KOI climbs
with it. That before → after arc — *lower cost from the model swap, higher pass rate from the prompt* —
is the whole point of optimizing the ratio rather than chasing raw cost or raw quality alone.

## Reproduce it yourself (a few cents)

The dashboard above needs nothing. To re-run the measurement against the live models:

```bash
cd samples/text_to_sql
echo 'OPENROUTER_API_KEY=sk-or-...' > .env     # .env is gitignored — never committed
nishiki measure --experiment .nishiki --mode run
nishiki koi-report --experiment .nishiki --web
```

`.env` is auto-loaded; the agent's source is never edited — Nishiki swaps the model in memory at the
choke point (`injection.choke: agent:generate_sql`). Prefer to be walked through it? From this dir just
run **`nishiki`** (no args) — the orchestrator drives init → measure → KOI report for you.

Want the **live** HUD? Wrap the agent with `nishiki run` — it reads the real token usage off each call
(no second request) and prints a rolling KOI:

```bash
nishiki run --experiment .nishiki --fresh --watch -- \
  python agent.py "How many products cost 500 dollars or more?"
```

## What's in here

```
agent.py            the AI agent under measurement (the choke point Nishiki reads + measures)
build_db.py         (re)generates shop.db — fully deterministic, all rows spelled out, no downloads
shop.db             the tiny synthetic retail DB (products / customers / orders / regions)
gold.jsonl          14 business questions with a gold SQL each (English, domain-neutral, authored here)
.nishiki/
  KOI.yaml          the recipe: task_type=sql, scorer=sql_result_match, floor=0.8
  MODELS.yaml       the candidate menu (route + price)
  runs/*.json       the measured run (what the #measured tab replays) — created by `nishiki measure`
```
