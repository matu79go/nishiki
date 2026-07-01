---
name: nishiki-koi
description: >
  Playbook to measure and maximize the KOI (KPI on Investment = the AI analog of ROI) of a target
  AI project. The orchestrator launched by `nishiki start` follows these steps, asking the user one
  question at a time and running the nishiki commands itself to generate the config set in the Nishiki
  area → measure KOI → produce the optimization table. Don't make the user type CLIs one by one.
---

# Nishiki KOI auto-optimization playbook (for orchestrator execution)

You (the orchestrator = claude code / codex / etc.) **execute these steps**. The human only answers
questions. **The config→command mapping is deterministic** (follow it as written below; don't improvise).
**Quality trade-offs are decided not by you but by the probe = real measurement (KOI).**

**[Language]** Detect the user's language and conduct the whole session — questions, explanations, and
artifacts (the prose in AGENT.md / KOI.yaml) — **in that language** (English if English). This playbook
is in English, but user-facing output must still be translated into the user's language when it differs.

Assumption (production scenario): the user **pip-installs `nishiki` once**, then just **`cd`s into the
project they want to measure and runs `nishiki start`**. All later commands are **cwd-independent**
(nishiki's own files resolve from the installed package; don't move the user into nishiki's source dir).
Specifically:
- nishiki's own modules resolve to absolute paths via `$(python3 -c 'import nishiki.X as _m; print(_m.__file__)')`.
- target-specific glue resolves to an absolute path via **`$(nishiki adapter-path <target>)`** (cwd-independent).

---

## Step 1 — Ask for the target project
Ask the user: "**What is the path of the target project to measure KOI for?** (the import root of that
AI code)" e.g. `/path/to/your/agent`. Hereafter `<TARGET>`.

## Step 1.5 — Existing profile? Offer a one-step **re-measure with the same models**
If `<target project root>/.nishiki/KOI.yaml` **already exists**, the user has measured before — do **not**
make them reconfigure from scratch. First **read `<DIR>/last_run.json`** (if present; written by a prior
`measure` / `calibrate-env`) and tell them what ran last time, e.g.
"Last time: 4 models [qwen-vl, qwen3-vl-32b, …] on batch 6472." Then offer a menu (the user may also
answer freely — you are flexible):

  ① **Re-measure with the SAME models** (quick path) — reuse `MODELS.yaml`/`KOI.yaml` candidates as-is.
  ② **Re-evaluate** only — just regenerate the KOI table from a past run (no new measurement).
  ③ **Change the models** — add another source (Step 2.5) or rebuild (`--force`, Step 2).
  ④ **Live KOI dashboard** — watch your running agent's KOI per call in real time (no new measurement, no
     charges) → **jump straight to Step 11**: wire the live capture for this user's run setup (host run /
     relay / **container overlay** as their environment requires), then launch `nishiki live --web`.

**If ① (re-measure same models)**, ask the two things that usually change each time — present as a menu,
**free-form answers OK**, then go straight to measurement (skip Steps 2–5; do not re-select source/candidates):
  - **What to measure this time?**
    · **same target as last time** (the same `batch_id` / gold file), or
    · a **new `batch_id`** (classification, `gold.source=batch`) → write it into `KOI.yaml` `gold.batch_id`
      (or override `NZ_GOLD_BATCH` for the glue), or
    · **new data / a new gold file** (extraction, `gold.source=file`) → pass `--gold-data <path>`.
  - **Mode?** · **probe** (cheap preview) · **full scored run**.
  Then jump to **Step 6.5** (it auto-branches: Path A glue, or Path B `nishiki measure`), reusing the saved
  candidate models. Confirm cost at the probe gate as usual before any billing.

- ② Re-evaluate → jump to Step 10 (KOI table) using the latest `runs/*_run.json` (or Step 4 summary first).
- ③ Rebuild → Step 2 (init gets `--force`). **Add source → Step 2.5 (additive merge)** — add the new source
  while **keeping** existing candidates / AGENT.md / KOI authoring.

If `.nishiki/` is absent, proceed to Step 2 as usual.
**Next time you can point at the same `.nishiki/` and re-run with a single answer.**

## Step 1.6 — If past runs exist, auto-suggest a "failure-band floor" (deterministic)
If `<DIR>/runs/*_run.json` (scored runs) **already exist**, before narrowing candidates, derive the
failure-band floor **mechanically**:
```
nishiki suggest-floor --experiment <DIR>
```
- Output = the models that fell below the floors and their prices, plus a **suggested `--min-price`
  (= cheapest model that passed; grab it from the `MIN_PRICE=` line)**.
- The floor value is **entirely derived from past-run numbers** (the AI does not guess quality). Your (the AI's)
  job is **only interpretation**: propose and get approval — "Last time <example failed model> fell below the
  floors → **filtering to ≥ \$X avoids wasting probe budget**. Filter with this?"
- Once approved, add `--min-price <X>` to subsequent init/--add (Step 2.5 / Step 3). **Don't emit it for an
  unmeasured target (no runs).**
- This prevents the accident of "**chasing only the cheapest and burning budget on the ultra-cheap band that
  wiped out in prior runs**." Inversions (cheap yet passing) automatically stay on the keep side.

## Step 2 — Ask for the candidate-model source
Ask: "**What is the source of candidate models?** ① bedrock (target uses Bedrock, stays in-country = for
sensitive data) / ② src (current model in the target code only) / ③ openrouter (cross-border = data routes
externally; for non-sensitive / light users)"
- If they seem unsure, advise briefly: for sensitive data like finance or PII, pick ① or ②. Don't send data
  that can't leave to openrouter.
- To measure **multiple sources together**, pass them comma-separated (e.g. `--source bedrock,openrouter`).
  To add later, use Step 2.5.
Hereafter `<SOURCE>`.

## Step 2.5 — **Additively merge** a source into an existing profile (--add)
*(Only when Step 1.5 ③ = a profile already exists and you want to "add" candidates from another source. New init is Step 3.)*

Ask which source to add (e.g. "Add openrouter models to the candidates too?"). Once decided, **the AI runs**:
```
nishiki init --target <TARGET> --source <added source> --add --out <DIR>
```
(For openrouter, `--openrouter-cap N` menus the N cheapest. Default 30. **Fetch = public API, no key needed, no charges.**)

The behavior of this `--add` (explain in one line to the user):
- **MODELS.yaml** = adds the new source **while keeping** existing candidates (src/bedrock/etc.); duplicates are first-wins (existing prioritized).
- **KOI.yaml** = **keeps** the authoring (choke/how/gold/cost_locus/floors/etc.) and updates only the candidate
  table (recomputed with the price cutoff = new source included) and `residency_bar`. **Adding openrouter makes
  residency_bar=unrestricted** (cross-border allowed).
- **AGENT.md** = untouched (keep the existing map). No AI authoring (no charges) is run either.

After running, the AI **presents the diff**:
1. The candidate-count change (e.g. "32→52 candidates, 20 of them openrouter") and the **M probe targets** after the price cutoff.
2. **★Cross-border gate (always, when openrouter was added)**: "openrouter **routes data abroad**. On the scored
   run, this target's input (e.g. document images) leaves to the outside. **Do you approve cross-border?**" → without
   approval, openrouter candidates are not run.
3. **Narrowing is proposed by the AI, and the user adjusts up/down by price** (raise/lower `--openrouter-cap`).
   **But make no quality judgment about "which model is better"** — whether a candidate stays is the price cutoff
   (deterministic); superiority is decided by **probe = real measurement (KOI)**. (Including one cheap candidate =
   one probe / wrongly excluding the winner = it can never be found. **When in doubt, keep it.**)
4. **★Failure-band floor (`--min-price`) = prevents chasing only the cheapest.** "Cheapest-first" alone wastes
   probe budget on **ultra-cheap, tiny models that wiped out in prior runs** (measured: nova-lite $0.06 / gemma-3-4b
   $0.04 fell below the floors at 44-53% Japanese-OCR match rate). **If past runs already reveal the failure band**,
   cut at its floor with `--min-price` (e.g. 0.1):
   `nishiki init … --source openrouter --add --min-price 0.1`
   - This is **not the AI guessing quality, but using a measured failure line** (distinct from a wrong-exclusion
     accident = grounded in numbers, not memory).
   - Don't add it for a target whose failure band is unmeasured (= cheapest-first as before). Use it **only when there
     is evidence (a fall in a past run)**.
   - The src current model is floor-exempt (always kept as the baseline).
→ Once agreed, go to Step 4 (3-file summary). Treat `<SOURCE>` as the post-add mix (bedrock+openrouter, etc.).

## Step 3 — init (profile generation) = deterministic mapping
**Run as-is**, per source:

- **src**: `nishiki init --target <TARGET> --source src --out <DIR>`
- **openrouter**: `nishiki init --target <TARGET> --source openrouter --out <DIR>`
- **bedrock**:
  - If the target **runs in a container** (AWS credentials are inside the container), fetch the catalog inside the container:
    1. Confirm the container name: `docker ps --format '{{.Names}}'` (the target's UI/app container, e.g. `your-container`). If unknown, ask the user. Hereafter `<CTN>`.
    2. Fetch the catalog (list API = **no charges**):
       ```
       { cat "$(python3 -c 'import nishiki.models_cmd as _m; print(_m.__file__)')"; printf '\nimport json as _j\nprint(_j.dumps(fetch_bedrock_catalog(region="ap-northeast-1", vision_only=True)))\n'; } \
         | docker exec -i -e AWS_REGION=ap-northeast-1 <CTN> python - > /tmp/nz_bedrock_catalog.json
       ```
    3. Pass the catalog to init:
       `nishiki init --target <TARGET> --source bedrock --catalog /tmp/nz_bedrock_catalog.json --out <DIR>`
  - If the target is not a container and AWS credentials are on the host, init directly without `--catalog` (boto3 required).

**By default, omit `--out` for the output `<DIR>`** → it saves to **`.nishiki/` directly under the target
project** (e.g. target=`…/example_target/app` → `…/example_target/.nishiki/`). This is the tool's metadata
dotfolder (like `.git`/`.vscode`, gitignored). **What read-only forbids is changing the target's code/DB/data/behavior** —
creating this `.nishiki/` is by-the-book and fine.
Output = `MODELS.yaml` (full candidate menu) / `AGENT.md` (source map) / `KOI.yaml` (execution spec).
Tell the user the real paths from init's output (`✓ MODELS.yaml → …`).

## Step 4 — Summarize the output [all 3 files] to the user
Summarize the role and contents of **all three** (don't skip even one):
1. **AGENT.md (source structure / map)**: what it is / choke point / cost structure, in 2-3 lines.
2. **MODELS.yaml (model candidates)**: N candidate menu → M probe targets (price cutoff) / price range $a–$b.
3. **KOI.yaml (this is "how it's measured") = ★always explain it plainly enough for a first-timer.** Don't just
   list jargon. Convey these 4 points in plain terms (if you use a term, always attach its meaning):
   - **(a) What it does for you (value)**: "It treats your current <current route> (e.g. qwen→sonnet cascade) as the
     **reference = today's best (100%)**, then searches for **alternative routes with a good cost/quality balance** and lists them."
   - **(b) What KOI is**: "KOI = **KPI achieved ÷ cost** = how much quality you get per dollar spent = the AI analog
     of cost-efficiency/ROI. With the current model at **1.0x**, candidates are ranked by 'how many times more
     cost-efficient than current' (same quality at half the price ≈ 2.0x)."
   - **(c) ★floors strategy (the core of our approach — always say it)**: "Computed naively, KOI has a **trap where
     cheaper always looks higher**, and an **ultra-cheap model that just answers randomly could rank #1 on the
     surface**. So the **cutoff (floors)** **disqualifies** the 'cheap-and-bad' — a floor on NG miss rate
     (`ng_recall_floor` = don't miss dangerous items) and a floor on match rate (`kpi_floor`). It surfaces the most
     cost-efficient **among those that pass the cutoff**. This is the core."
   - **(d) ★mode 1's ceiling**: "gold = current output → **current is the upper bound (1.0x)**. All you can measure is
     'same quality, cheaper.' Beating current / finding errors requires **mode 2 (human labels)**."
- **★Show the probe trial cost as a dollar figure** (present init's "probe estimate ≈ $X" as-is). One note that
  probe cost changes as you add/remove candidates. Let the user decide the candidate count **by dollars**.
- Then confirm: "**We'll measure these M. Change the candidates (KOI.yaml candidates)?**"

## Step 5 — KPI/ceiling agreement gate (before any cost)
Get the user's agreement on the KPI comparison method explained in Step 4 and on **mode 1's ceiling**:
"Proceed with this measurement method (current = upper bound, search for same quality cheaper)?"
- Different / want to beat current → edit KOI.yaml or branch to **mode 2 (human KPI labeling)**.
- Agreed → next (confirm gold → billing gate).

## Step 6 — Confirm gold (the baseline)
Branch on KOI.yaml's `task_type` and gold:
- **classification (mode 1, gold.source=batch)**: if `gold.batch_id` is `TBD`, ask
  "**Which current batch is the baseline (gold)?** (the current model's finalized output; zero re-run cost)" e.g.
  example_target: the audit batch number. Write the value into KOI.yaml.
- **extraction etc. (mode 2, gold.source=file)**: if `gold.data` is `TBD`, ask
  "**Path to the ground-truth dataset (expert annotations, etc.)?**" e.g. CUAD: `/…/CUADv1.json`. In Path B, pass it
  with `--gold-data` (you may also write it in KOI). **Don't put data in the repo** (keep it only on your data host, etc.).

## Step 6.5 — Decide the measurement path (auto-branch by reading KOI.yaml)
- KOI.yaml **has a `run:` block** = init authored a **generic-adapter config** (task_type/prompt/parser/gold_format)
  → **Path B: `nishiki measure`** (no target-specific glue, no container; classification or non-classification).
- **No `run:`** (= a classification/container special form like example_target requiring target DB/module dict swaps)
  → **Path A** (Steps 7–10).

---

## Path B — Measure with the generic adapter (custom agents / dataset-style, has a `run:` block)
No target-specific glue or container needed. `nishiki measure` measures using KOI.yaml's run config + MODELS.yaml.

**B-1. Verify the wiring at $0 (always first)**:
```
nishiki measure --experiment <DIR> --mode run --dry-run --gold-data <GOLD> --limit 5
```
Without calling real models (zero cost), confirm load→prompt→parse→scoring→save passes. On error, fix the config
(the field names in gold_format/parser/prompt). **Only go to billing once this passes.**

**B-2. probe (micro-charge gate)**: with `--limit <N>` (the expected count of the scored run), trial-run each
candidate and project cost onto N items:
```
nishiki measure --experiment <DIR> --mode probe --gold-data <GOLD> --limit <N>
```
- **★If there are cross-border (openrouter = openai_compatible) candidates**: `export OPENROUTER_API_KEY=…` is required
  + get **cross-border approval** (data routes externally). bedrock candidates use AWS credentials (env). You should have
  confirmed the candidate list with `--dry-run`.
- Present the output's `probe actual $X / full projection $Y` and ask "**Run the scored run?**" → **don't execute until explicit GO**.

**B-3. Scored run (billing, re-gate)**:
```
nishiki measure --experiment <DIR> --mode run --gold-data <GOLD> --limit <N>
```
The run JSON is saved to `<DIR>/runs/<stamp>_run.json`. → **B-4 = go to Step 10 (KOI optimization table)** (shared).
- Non-classification has **no reference (current route)** → the KOI table ranks not by "vs current" but by the absolute
  **F1÷cost** (tell the user so).

---

## Path A — Measure with container-specific glue (example_target style, classification, no `run:` block)

## Step 7 — Build the measurement env + cost gate (probe)
1. Run `nishiki calibrate-env --experiment <DIR>`
   → obtain `NZ_CANDIDATES` / `NZ_GOLD_BATCH` / `NZ_CATALOG_FILE` (/tmp/nz_catalog.json).
   ※ The **reference (current = CASCADE)** is auto-prepended to NZ_CANDIDATES = for computing vs-current (1.0x).
     Running current once adds a little cost (its value = being able to state "how many times more cost-efficient than current" each candidate is).
2. Present the probe estimate **in dollars**: "Trial-running just N items per candidate = about $X. Estimate for the
   full scored run = about $Y. **Run the probe?**" → **don't execute until explicit user GO** (billing gate).

## Step 8 — Run probe (micro-charge)
Target-specific glue (example_target style = `adapters/example_target/calibrate.py`) via stdin inside the container.
**read-only** (SELECT only; model swap is in-memory at runtime only):
```
docker exec -i -e NZ_MODE=probe -e NZ_PROBE_N=3 \
  -e NZ_GOLD_BATCH=<batch> -e NZ_CANDIDATES="<NZ_CANDIDATES>" \
  -e NZ_CATALOG="$(cat /tmp/nz_catalog.json)" \
  <CTN> python - < "$(nishiki adapter-path example_target)"
```
**★If it includes openrouter (openai_compatible) candidates** (when calibrate-env emitted "⚠ cross-border candidate"):
**add** `-e OPENROUTER_API_KEY="$OPENROUTER_API_KEY"` to the docker exec (the glue needs it to call via OpenRouter).
Without the key, openrouter candidates all error. **Cross-border approval (Step 2.5) is a prerequisite.** The glue
auto-routes each candidate between Bedrock (converse) / OpenRouter (openai-compatible) in `call`, so the mix measures in one pass.

stderr = progress table, stdout = result JSON. In the report, **always**: ① candidates with many errors/none (= can't
be called on that route), ② **★the scored-run total estimate (all candidates × all items ≈ $X) = the total that will
actually be billed**, shown at a glance (probe-measured basis).

## Step 9 — Scored run (billing, re-gate) + always save the result
**Prominently present** the **scored-run total the probe measured ($X = all candidates × all items)** and ask "Run the
scored run at this total?" → on **explicit GO**, docker exec with `NZ_MODE=run` (no PROBE_N needed). **Always save the
result JSON (stdout)**:
```
mkdir -p <DIR>/runs
docker exec -i -e NZ_MODE=run -e NZ_GOLD_BATCH=<batch> -e NZ_CANDIDATES="<…>" \
  -e NZ_CATALOG="$(cat /tmp/nz_catalog.json)" <CTN> python - < "$(nishiki adapter-path example_target)" \
  > <DIR>/runs/<batch>_run.json
```
(If it includes openrouter candidates, add `-e OPENROUTER_API_KEY="$OPENROUTER_API_KEY"` as in probe.)
(You may also offer to drop expensive candidates to lower the total.) Keep the probe result too at `<DIR>/runs/<batch>_probe.json`.

## Step 10 — KOI dashboard (the ONE result UI) ★don't settle for terminal text
From the saved scored-run JSON, bring up the **live KOI dashboard** — the single place the user looks.
**Always run with `--web`**; the launch is deterministic (the CLI starts the browser dashboard in the
background and prints its URL — you do not "decide" to offer it):
```
nishiki koi-report --experiment <DIR> --web   # latest run in <DIR>/runs/ → dashboard at http://127.0.0.1:8765
```
(`--no-open` to not open a browser, `--port N` to change the port.)
- **Present ONLY the dashboard URL** (`http://127.0.0.1:<port>`). **Never mention `koi_report.html`, a
  "static HTML", or "open in a browser/Live Server" to the user.** The dashboard fully supersedes the static
  file (same leaderboard + per-call charts); the `.html` is just a persisted artifact, not a user choice.
- Add 1-2 terminal lines of key points (recommended model / floors-disqualified group) and a conclusion
  ("staying equivalent to current, <model> gives about <multiplier>, $/item <a>→<b>").
- **If the top is closely contested (e.g. two models differ by 1 NG), explicitly state "recommend multiple runs to confirm"** (a single ranking is non-deterministic and fluctuates).
- Results (runs/*.json) persist in `.nishiki/` = **next time you can skip init and re-evaluate/compare**.
- **★ The dashboard is up; now go to Step 11** to make it *live* (wire the capture so per-call KOI updates
  as the agent runs). Don't end the turn at a one-shot view without offering the live capture.

## Step 11 — Live KOI (optional) — instant per-call KOI while the agent runs, **no model calls**
Offer this after the table: "**Want a live KOI dashboard while your agent runs?** It shows each call's KOI in
real time (estimated from this run; no model calls, no charges)."

**The GOAL is the same for every user: a live, dynamically-updating KOI dashboard.** What differs is only
*how the agent's calls reach `.nishiki/live.jsonl`* — and bridging that is **your** job (the AI), per the
user's setup. The user edits no source and writes no config; you read what you need and wire it.

**The view = `nishiki live --web`** (the browser dashboard: cards + per-call charts comparing every
candidate + floor-aware leaderboard; serves `127.0.0.1:8765`, `--port`/`--no-open`, offline, no model
calls). Prefer it as the default; offer `nishiki live` (terminal HUD) only if the user wants a no-browser
view. **Two halves, always in this order: (1) wire the capture (below) so calls flow to `live.jsonl`,
then (2) launch `nishiki live --web`.**

When run non-interactively (i.e. by you), `nishiki live --web` self-detaches (non-blocking) and prints the
dashboard URL plus two deep links — **first measurement `…/#measured`** and **Live (per call) `…/#live`**.
**Present those URLs to the user verbatim, exactly as the command printed them** (it has one screen with two
tabs). Don't replace them with just the base URL or invent your own wording for the links.

Always-true pieces (generic, user-independent):
- The model-call site is `injection.choke` in KOI.yaml (auto-detected). Exact usage is read from the
  choke's return when present; a raw `model_id` is auto-mapped to the candidate key.
  `nishiki estimate --prompt … [--image …]` gives a one-off what-if.

Pick the capture method that fits the user's run environment (always finishing by launching `nishiki live --web`):
- **Agent launched as a host process** (script / host server) → `nishiki run --experiment <DIR> --watch --
  <the command that starts the agent>` (one terminal: agent + HUD). Wraps the choke in-process, writes only
  to `.nishiki/live.jsonl`, source untouched, provider-agnostic. This is the generic path. For the browser
  dashboard, run the agent with `nishiki run` (no `--watch`) and `nishiki live --web` alongside.
- **Choke returns no usable usage** → author a tiny `.nishiki/probe_<target>.py` (read the choke's
  source → map args/return to `{model, in_tokens, out_tokens}`) and pass `--probe probe_<target>:func`.
- **Agent calls an OpenAI-compatible endpoint** and you can't wrap the process → `nishiki relay` and point
  its base URL at the relay (env only).
- **Agent runs in a container** (docker compose, etc.) → the host `nishiki run` can't reach inside, so
  **author an opt-in overlay** for the user's setup: read their compose, and for the agent service mount
  the nishiki package (read-only) + a tiny `sitecustomize.py` that calls `nishiki.autoprobe.install()`,
  set `PYTHONPATH`, `NZ_PROBE_CHOKE`, `NZ_PROBE_LOG=<mounted .nishiki>/live.jsonl` — command unchanged, no
  rebuild (the probe needs only stdlib). Fill in the service name/paths from *their* compose; never hardcode
  another project's names.
- **Whatever the capture path, finish at the dashboard:** `nishiki live --web --experiment <DIR>` (it tails
  the same `.nishiki/live.jsonl` the capture writes; for a container, point it at the mounted `.nishiki`).

Keep the **full-automation** spirit: the human just runs the agent; the AI did the wiring. Never make the
user hand-edit source or hunt for the call signature — read it from the choke yourself, and reach the same
goal (a live KOI dashboard) whatever their setup.

---

## Guardrails (absolute)
- The target is **read-only**. Cause no writes or side effects on production processing.
- **Before any charge (probe/scored run), always present the dollar amount → explicit user GO.** Don't add models on your own and bill.
- **Respect residency_bar**: if your_cloud, exclude external (openrouter etc.) before the scored run.
- Take model names and prices **from the catalog/code** (don't invent from memory). For unknown prices, declare "price needed".
- Report each step's result concisely in the user's language, then move to the next question.
