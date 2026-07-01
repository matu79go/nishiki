# CLAUDE.md — Nishiki

**Start here (maintainer, local only):** read `docs/TODO.md` first (current state + the public-release
gates). Note: `docs/` is internal and gitignored — it is not part of the public repo.

Nishiki = the KOI optimizer: measure & maximize KPI ÷ cost (quality per dollar) of any LLM agent,
for classification and non-classification KPIs. Core flow: `nishiki start` → init (auto-detect KPI from
source) → measure → koi-report.

## Build / test
- `pip install -e .` (Python ≥3.9). Tests use no framework:
  `python3 -m tests.test_scoring` / `test_runner` / `test_generic_adapter` / `test_assembler` /
  `test_cuad` / `test_dotenv` / `test_smoke`.
- Each step: keep all suites green.

## Hard rules
- **Repo goes PUBLIC.** Never commit secrets/data/weights or any client identifier (names, containers,
  tables, internal paths). It was de-cliented once — keep a scan at zero. `.nishiki/`, `.env`, `data/`
  are gitignored.
- **English is the primary language** of code/docs/CLI. (Converse with the user in their language —
  usually Japanese.)
- **Verify the real CLI flow** (`nishiki init` / `start` / `measure`) by running it and reading the
  output before claiming done — unit tests passing is not enough.
- `claude -p` is the Claude Code agent, **not** a clean model backend — don't use it to measure a model.

## Layout
`src/nishiki/` (cli + generic_adapter/scoring/runner/backends/init_cmd/models_cmd/koi_report) ·
`adapters/` (bespoke per-target glue; public ones only) · `tests/` · `docs/` (internal, gitignored).
