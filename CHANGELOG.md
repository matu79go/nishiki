# Changelog

All notable changes are documented here. Versioning follows [SemVer](https://semver.org/):
`MAJOR.MINOR.PATCH`. While `0.x`, the API may change between minor versions.

## [0.1.0] — 2026-06-22

Initial public release.

### Added
- `nishiki start` — orchestrator-driven flow: read your agent's source → init → measure → KOI report.
- **Generic, config-driven measurement** (`GenericAdapter`) covering **classification and
  non-classification** KPIs from one framework (no per-target hand-coding for the common case).
- **Auto KPI detection from source**: the orchestrator infers `task_type`, scorer, choke point, and
  prompt/parser and writes them into `KOI.yaml`.
- **Scorer registry**: `label_match` (classification → agreement rate) and `span_f1`
  (extraction → character/token overlap; whitespace/case-tolerant matching).
- **Price-stratified candidate menus** spanning cheap → frontier across OpenRouter and Bedrock.
- `nishiki measure` (with `--dry-run` for $0 wiring checks), `nishiki koi-report` (HTML with a
  draggable floor slider), `nishiki suggest-floor` (deterministic failure-band price floor).
- **Floors** strategy: disqualify "cheap but bad" below `kpi_floor` (and optional `ng_recall_floor`
  for classification), then rank survivors by KOI = KPI ÷ cost.

### Known limits
- The `nishiki start` auto-launch needs `claude` or `codex` installed on the machine; otherwise drive
  the underlying CLI (`init` / `measure` / `koi-report`) directly.
- CLI output is currently Japanese; English i18n is pending (the orchestrator already replies in the
  user's language).
- Bespoke targets that need a live DB/container for scoring keep a thin per-target adapter under
  `adapters/`.
