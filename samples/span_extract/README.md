# Sample: span extraction — a non-classification KPI, measured by KOI

A tiny, self-contained example you can open in **one command** — no API key, no charges. It shows the
whole point of Nishiki on a small scale: *which model gives the most quality per dollar?*

## 1. The task (what this agent does)

`agent.py` is a small AI agent. Given a **question** and a short **passage**, it asks a model to return
the *verbatim* fragment of the passage that answers the question — no paraphrase.

```
Passage:  "The Amazon River ... flows through South America and empties into the Atlantic Ocean ..."
Question: "Which ocean does the Amazon River empty into?"
Answer:   "the Atlantic Ocean"      ← a span copied straight out of the passage
```

The agent's model call lives in one function, `extract_span` — that's the **choke point** Nishiki
splices candidate models into (in memory; the source file is never edited).

## 2. The KPI — Nishiki decides it for you

You don't tell Nishiki how to grade this. Its `init` step **reads the agent's source and auto-detects
the KPI**: it sees the agent pulls a *span of text out of a passage* rather than choosing from a fixed
set of labels, so it classifies the task as **`extraction`** and picks the matching scorer —
**span-overlap F1** (how much the returned characters overlap the gold answer, 0–1). This is a
**non-classification** KPI: a graded quality score, not a right/wrong label.

The bundled `.nishiki/KOI.yaml` encodes exactly that recipe — `task_type: extraction` +
`scorer.kpi: span_f1`, together with a **quality floor** of `0.55` (the minimum span-F1 a model must
reach to even qualify). `nishiki init` finds the call site on its own (it's a standard
`chat.completions.create`) and the orchestrator classifies the task from the source. (A task that
emitted one of a fixed label set would instead be auto-detected as `label_match` — same framework,
different scorer.)

## 3. The result — see it on the web

```bash
cd samples/span_extract
nishiki koi-report --web        # opens a browser dashboard — no key, no model calls
```

The dashboard makes **zero model calls** and costs nothing — it replays the bundled results in
`.nishiki/`. Two tabs:

- **`/#measured`** — the leaderboard below, from `.nishiki/runs/*.json`.
- **`/#live`** — per-call KOI as the agent runs, from a bundled `.nishiki/live.jsonl` (6 real deepseek
  calls). `nishiki history --experiment .nishiki` prints the same per-call view in the terminal.

### Reading the table

`KOI = KPI ÷ cost` = quality per dollar. First the floor disqualifies anything below span-F1 0.55;
among the survivors, Nishiki ranks by KOI.

| model (route) | KPI (span F1) | $ / item | KOI | what it means |
|---|---:|---:|---:|---|
| **deepseek** (`deepseek/deepseek-v4-pro`) | 0.79 | $0.00016 | **5007** | **best value — clears the bar, dirt cheap** |
| glm46 (`z-ai/glm-4.6`) | 0.55 | $0.00158 | 350 | just clears the floor |
| gpt4 (`openai/gpt-4`) | 0.76 | $0.00343 | 221 | good, mid-priced |
| gpt54pro (`openai/gpt-5.4-pro`) | **0.94** | $0.01872 | 50 | highest quality, worst value |

- **KPI** = measured span-F1 over 12 questions (higher = better extractions).
- **$/item** = list-price cost to process one question.
- **KOI** = KPI ÷ $/item = quality per dollar. **Higher wins.**

**The takeaway:** the frontier model is the quality king (0.94) and the *worst* value. **deepseek
delivers 84% of the frontier's quality at 1/117th the cost**, so its quality-per-dollar is ~100× higher.
That is exactly why you optimise the *ratio*, not raw cost — and why the floor matters: without it, an
ultra-cheap model that returns garbage would look like the best bargain.

*(Prices are OpenRouter list prices in USD per million tokens, captured live with `nishiki models`.
KPI/cost are from the bundled run; your own numbers vary with the models and data you measure.)*

## Reproduce it yourself (optional — a few cents)

The dashboard above needs nothing. To re-run the measurement against the live models:

```bash
cd samples/span_extract
echo 'OPENROUTER_API_KEY=sk-or-...' > .env     # .env is gitignored — never committed
nishiki measure --experiment .nishiki --mode run
nishiki koi-report --experiment .nishiki --web
```

The bundled run cost about **$0.29** in total (mostly the frontier model). `.env` is auto-loaded; the
agent's source is never edited — Nishiki swaps the model in memory at the choke point
(`injection.choke: agent:extract_span` in `.nishiki/KOI.yaml`).

## What's in here

```
agent.py            the AI agent under measurement (the choke point Nishiki reads + measures)
gold.json           12 domain-neutral questions with gold answer spans (SQuAD format, authored here)
.nishiki/
  KOI.yaml          the auto-detected recipe: task_type=extraction, scorer=span_f1, floor=0.55
  MODELS.yaml       the candidate menu (route + price)
  runs/*.json       the bundled measured run (what the #measured tab replays)
  live.jsonl        6 real per-call events (what the #live tab replays)
```
