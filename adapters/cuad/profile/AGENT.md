# AGENT.md — map of the cuad (contract-clause extraction) adapter

A verification bed proving Nishiki can measure a "non-classification KPI (extracted-span F1)". Written with the
**same runner.Adapter contract** as example_target (classification = verdict match) = the generalization proof
that you can measure any target just by adding an adapter (design doc §18.9, 2026-06-22).

## Target = our thin self-built agent
example_target is an existing business app (the choke lives inside the counterpart's code), but CUAD is a
**thin self-built agent**. A clean choke = lets us focus on the essence of generalization (model swap, measurement,
scoring, KOI). Layering over a heavyweight framework (LangChain family) is the hard part, deferred to last
(TODO 2026-06-22).

- Implementation: `adapters/cuad/calibrate.py`
- choke = `extract_clause(context, question, model_id)` … asks the LLM once to "extract this clause verbatim"
- scoring = `scoring.span_overlap_f1` … overlap F1 over character-offset sets (language-neutral = works on English contracts too)
- runner = `nishiki.runner` (the shell shared with example_target)

## Data (CUAD)
- CUAD v1 = 510 contracts × 41 clause questions, expert span annotations. **CC BY 4.0** (The Atticus Project).
- SQuAD-format JSON. `data[].paragraphs[].qas[].answers[].answer_start` is the gold span.
- **Real data is kept only on your data host** (do not put it in the repo or locally). Observe the attribution.
- Obtain (on the data host): official https://www.atticusprojectai.org/cuad → `CUADv1.json`. Pass the absolute path to NZ_DATA.

## Install (the data host = Python 3.12 forbids system pip under PEP 668 → venv)
```
cd /path/to/nishiki
python3 -m venv .venv && . .venv/bin/activate
pip install -e .            # this makes the `nishiki` command available
# (if not using a venv, PYTHONPATH=src python3 -m nishiki … also works)
```

## Run (local, with nishiki pip-installed)
```
# 1) Assemble the measurement env (MODELS.yaml/KOI.yaml → NZ_CATALOG/NZ_CANDIDATES)
nishiki calibrate-env --experiment adapters/cuad/profile --catalog-out /tmp/nz_cuad.json

# 2) probe (small-charge preview, N items per candidate)
NZ_MODE=probe NZ_PROBE_N=3 NZ_DATA=/path/to/CUADv1.json \
  NZ_CANDIDATES="gpt-5-nano,llama-4-scout,qwen3-vl" NZ_CATALOG="$(cat /tmp/nz_cuad.json)" \
  OPENROUTER_API_KEY=... python "$(nishiki adapter-path cuad)"

# 3) scored run (all items, scoring) → save the run JSON
NZ_MODE=run NZ_LIMIT=50 NZ_DATA=/path/to/CUADv1.json \
  NZ_CANDIDATES="gpt-5-nano,llama-4-scout,qwen3-vl" NZ_CATALOG="$(cat /tmp/nz_cuad.json)" \
  OPENROUTER_API_KEY=... python "$(nishiki adapter-path cuad)" \
  > adapters/cuad/profile/runs/$(date +%s)_run.json

# 4) KOI optimization table (non-classification = the NG slider auto-hides)
nishiki koi-report --experiment adapters/cuad/profile
```

## Cost sense from real runs (2026-06-22 probe, 5 items/candidate, balanced n=13,404)
| model | /item | all items (13,404) projected | p50 latency |
|---|---|---|---|
| gpt-5-nano | $0.00088 | **$11.8** | 14.3s (slow = watch out) |
| llama-4-scout | $0.00107 | **$14.3** | 1.2s (fast) |
| qwen3-vl-235b | $0.00217 | **$29.1** | 3.7s |

- probe actual cost is **$0.02** (15 calls). **3 candidates × all items ≈ $55**. With `NZ_LIMIT=200`, ≈ $1-2.
- A full contract = one item ~14K input tokens. All items is expensive → **always narrow with NZ_LIMIT, probe, then do the scored run**.

## Notes
- Many CUAD contracts have long full texts. Narrow the item count with `NZ_LIMIT`, check the total in a probe first, then do the scored run.
- There is no reference (current path) = no 1.0x-vs-current figure. KOI ranks by the absolute value of F1÷cost.
- Cross-border = via OpenRouter (public data, so residency_bar: unrestricted).
