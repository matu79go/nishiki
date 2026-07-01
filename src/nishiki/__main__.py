"""nishiki CLI — entry point for the KOI optimizer (production flow = `nishiki start`).

Main commands: start (launch orchestrator) / init (auto-generate config, add source) /
calibrate-env (measurement env) / suggest-floor (failure-band floor) / koi-report (KOI table) / models / setup / config / ask.
The old providers/pipeline generic framework (calibrate/run/watch/report) was moved to attic/ on 2026-06-21.
"""
import argparse
import os
import sys




def _print_brain_menu(orchestrator):
    print("First choose the orchestrator brain (whichever you have):\n")
    labels = {
        "claude_p": "claude -p (Claude Code subscriber)",
        "codex": "OpenAI Codex CLI",
        "copilot": "GitHub Copilot CLI",
        "openrouter": "OpenRouter (proprietary+open with one key; no subscription needed)",
    }
    for name, ok, dep in orchestrator.available_brains():
        mark = "✓ available" if ok else f"— not found ({dep})"
        print(f"  {name:12} {labels.get(name, ''):42} [{mark}]")
    print("  command      specify any CLI yourself (e.g. codex/copilot/in-region)")
    print("\nselect: nishiki config --brain claude_p")
    print("     nishiki config --brain openrouter --model anthropic/claude-opus-4.6-fast")
    print("     nishiki config --brain command --argv 'codex exec {prompt}'")


def _ask(prompt, default=""):
    try:
        ans = input(prompt).strip()
    except EOFError:
        ans = ""
    return ans or default


def _cmd_setup(argv):
    """First-run wizard: interactively pick brain / candidate source / residency bar and save (no flags needed afterward)."""
    from . import orchestrator
    print("=== Nishiki setup (first run only; no flags needed afterward) ===\n")
    cfg = orchestrator.load_config()

    # 1) brain
    brains = orchestrator.available_brains()
    print("[1/3] Choose the orchestrator brain (the LLM that writes the config):")
    for i, (name, ok, dep) in enumerate(brains, 1):
        print(f"   {i}) {name:11} [{'✓ available' if ok else '— not found:'+dep}]")
    print(f"   {len(brains)+1}) command   (specify any CLI yourself)")
    sel = _ask(f"   number [1]: ", "1")
    try:
        idx = int(sel) - 1
        cfg["brain"] = brains[idx][0] if idx < len(brains) else "command"
    except (ValueError, IndexError):
        cfg["brain"] = "claude_p"
    if cfg["brain"] == "openrouter":
        cfg["model"] = _ask("   OpenRouter model id [anthropic/claude-opus-4.6-fast]: ",
                             "anthropic/claude-opus-4.6-fast")
    if cfg["brain"] == "command":
        cfg["argv"] = _ask("   launch command (must contain '{prompt}') [codex exec {prompt}]: ",
                           "codex exec {prompt}").split()

    # 2) candidate model source
    print("\n[2/3] KOI candidate model source:")
    print("   The current model (extracted from code) is always included.")
    inc = _ask("   Also have OpenRouter alternatives suggested? [y/N]: ", "n").lower()
    cfg["suggest_models"] = inc.startswith("y")

    # 3) data residency bar
    print("\n[3/3] Data residency policy (handling of external routes):")
    print("   1) Up to your own cloud (exclude external = OpenRouter etc. from candidates) ← for financial/sensitive data")
    print("   2) Unrestricted (include external routes in the cost comparison too)")
    bar = _ask("   number [1]: ", "1")
    cfg["residency_bar"] = "your_cloud" if bar != "2" else "unrestricted"

    path = orchestrator.save_config(cfg)
    print(f"\nSaved → {path}")
    print(f"  brain={cfg['brain']} / OpenRouter candidates={'yes' if cfg['suggest_models'] else 'no'} "
          f"/ residency={cfg['residency_bar']}")
    print("→ From now on you can run `nishiki init --target <dir>` without flags.")


def _cmd_config(argv):
    from . import orchestrator
    p = argparse.ArgumentParser(prog="nishiki config")
    p.add_argument("--brain", default=None, choices=orchestrator.BRAINS, help="choose the brain to use")
    p.add_argument("--model", default=None, help="model id for the openrouter brain")
    p.add_argument("--argv", default=None, help="launch command for brain=command (must contain '{prompt}')")
    args = p.parse_args(argv)

    if not args.brain:
        cur = orchestrator.configured_brain()
        print(f"current brain: {cur or '(none selected)'}\n")
        _print_brain_menu(orchestrator)
        return
    cfg = orchestrator.load_config()
    cfg["brain"] = args.brain
    if args.model:
        cfg["model"] = args.model
    if args.argv:
        cfg["argv"] = args.argv.split()
    path = orchestrator.save_config(cfg)
    print(f"Set brain to [{args.brain}] → {path}")


def _cmd_init(argv):
    from . import init_cmd
    p = argparse.ArgumentParser(prog="nishiki init")
    p.add_argument("--target", required=True, help="root of the target project to diagnose")
    p.add_argument("--out", default=None,
                   help="output location for the generated config. Default with --source = .nishiki/ "
                        "directly under the target project (tool meta dotfolder = gitignored; the target's code/DB/data is never touched)")
    p.add_argument("--source", default=None,
                   help="candidate model source (bedrock / openrouter / comma-separated "
                        "'bedrock,openrouter'). When given, the auto-assembler generates the full "
                        "MODELS.yaml/AGENT.md/KOI.yaml set (§18.9)")
    p.add_argument("--add", action="store_true",
                   help="**additively merge** --source into an existing profile (production use). "
                        "Keeps existing candidates/AGENT.md/KOI authoring and only updates the new-source candidates and candidate table")
    p.add_argument("--no-author", action="store_true",
                   help="with --source, skip AI authoring (AGENT.md/quality_hint) and generate the deterministic part only")
    p.add_argument("--openrouter-cap", type=int, default=30,
                   help="number of vision models to put in the menu with --source openrouter (cheapest first; default 30)")
    p.add_argument("--min-price", type=float, default=None,
                   help="floor price for probe candidates ($/Mtok). Excludes the ultra-cheap band that yields no real quality "
                        "(prior case: nova/gemma wiped out on Japanese OCR) from probing. e.g. 0.1. The current model from src is exempt")
    p.add_argument("--catalog", default=None,
                   help="pre-fetched catalog JSON (fetch_bedrock_catalog format). "
                        "When the target runs in a container with AWS auth there, the AI fetches it inside the container and passes it in")
    p.add_argument("--dry-run", action="store_true",
                   help="don't call claude; just show the discovered code fragments (choke/pricing/cascade)")
    p.add_argument("--suggest-models", action="store_true",
                   help="also suggest alternative models fetched live from OpenRouter (via external route = requires data residency check)")
    p.add_argument("--backend", default=None,
                   help="override the brain for this run only (default = the one chosen via nishiki config)")
    p.add_argument("--orchestrator-model", default=None,
                   help="model id to use for the openrouter brain (default = a strong Claude; check with nishiki models)")
    p.add_argument("--force", action="store_true",
                   help="overwrite existing config (which may have been hand-edited)")
    args = p.parse_args(argv)
    from . import dotenv
    dotenv.autoload(args.target)   # also pick up the target project's own .env

    # Resolve default output location: Nishiki area = .nishiki/ directly under the target project
    # (tool metadata dotfolder; gitignored like .git/.vscode. The target's code/DB/data is never touched = read-only).
    # If the target is under an import dir like app/src, place .nishiki in its parent = the project root.
    if args.out is None:
        if args.source:
            ap = os.path.abspath(args.target).rstrip("/")
            root = (os.path.dirname(ap)
                    if os.path.basename(ap) in ("app", "src", "source", "lib", ".")
                    else ap)
            args.out = os.path.join(root, ".nishiki")
        else:
            args.out = "nishiki_experiment"

    # --add = additively merge a new source into an existing profile (production use; don't break existing)
    if args.add:
        if not args.source:
            print("--add requires --source (the source to add). e.g. --source openrouter --add")
            return
        if not os.path.exists(os.path.join(args.out, "MODELS.yaml")):
            print(f"No existing profile: {args.out}/MODELS.yaml")
            print("  → First create one with a normal init (nishiki init --target … --source bedrock).")
            return
        print(f"[nishiki init --add] additively merging source={args.source} into {args.out}…")
        add_catalog = None
        if args.catalog:
            import json as _json
            with open(args.catalog, encoding="utf-8") as _f:
                add_catalog = _json.load(_f)
            print(f"  using pre-fetched catalog of {len(add_catalog)} ({args.catalog})")
        written = init_cmd.merge_source(
            args.out, args.source, target=args.target,
            openrouter_cap=args.openrouter_cap, min_price=args.min_price,
            catalog=add_catalog)
        for name, path in written.items():
            print(f"  ✓ {name:11} → {path} (kept existing and updated)")
        print("  * AGENT.md unchanged (existing map kept). Next: calibrate-env → calibrate.")
        return

    # Edit protection: don't silently overwrite an experiment dir that already has config (don't clobber tweaks)
    guard = "KOI.yaml" if args.source else "pipeline.yaml"
    existing = os.path.join(args.out, guard)
    if not args.dry_run and os.path.exists(existing) and not args.force:
        print(f"Config already exists: {existing}")
        print("  Aborted to avoid overwriting your hand edits.")
        print("  → To regenerate use --force / for another location --out <dir> / or edit directly and go to calibrate")
        return

    if args.dry_run:
        found = init_cmd.discover(args.target)
        for key in ("choke", "catalog", "pricing", "cascade"):
            print(f"\n===== {key}: {len(found[key])} found =====")
            for s in found[key][:4]:
                print(s)
                print("-" * 60)
        print("\n[--dry-run] claude not called. To actually generate, drop --dry-run.")
        return

    from . import orchestrator
    cfg = orchestrator.load_config()
    brain = args.backend or cfg.get("brain")

    # --source given = auto-assembler (MODELS.yaml deterministic + AGENT.md/KOI.yaml AI-authored)
    if args.source:
        author = not args.no_author
        if author and not brain:
            print("AI authoring (AGENT.md) needs a brain. Run `nishiki setup` or use --no-author.")
            return
        catalog = None
        if args.catalog:
            import json as _json
            with open(args.catalog, encoding="utf-8") as _f:
                catalog = _json.load(_f)
            print(f"[nishiki init] using pre-fetched catalog of {len(catalog)} ({args.catalog})")
        mode = "deterministic only" if not author else f"deterministic + AI-authored[{brain}]"
        print(f"[nishiki init] auto-assembler source={args.source} ({mode})…")
        written = init_cmd.generate_profile(
            args.target, source=args.source, out_dir=args.out,
            backend=args.backend, model=args.orchestrator_model, author=author,
            openrouter_cap=args.openrouter_cap, catalog=catalog, min_price=args.min_price,
        )
        for name, path in written.items():
            print(f"  ✓ {name:11} → {path}")
        print("→ Review the contents (especially KOI.yaml gold.batch_id / quality_hint). Next: calibrate.")
        return

    if not brain:
        print("No brain selected. Run the first-time setup first:\n")
        print("    nishiki setup\n")
        return
    # If no flags, default to the saved config values (= no need to type flags every time)
    suggest = args.suggest_models or cfg.get("suggest_models", False)
    src = "code + OpenRouter live candidates" if suggest else "code"
    print(f"[nishiki init] discovering {src} → generating config with brain[{brain}]…")
    obj, _found = init_cmd.generate(
        args.target, suggest_models=suggest,
        backend=args.backend, model=args.orchestrator_model,
    )
    out = init_cmd.write_experiment(args.out, obj)
    print(f"choke point : {obj.get('choke','?')}")
    print(f"experiment config → {out}/ (providers.yaml / pipeline.yaml / NOTES.md)")
    print("→ Review the contents and tweak if needed. Next: calibrate (with cost gate).")




def _cmd_models(argv):
    from . import models_cmd
    p = argparse.ArgumentParser(prog="nishiki models")
    p.add_argument("--vision", action="store_true", help="restrict to vision-capable models only (for OCR)")
    p.add_argument("--search", default=None, help="filter by substring match on id/name (e.g. claude, qwen)")
    p.add_argument("--include-free", action="store_true", help="also include :free and price-0 entries")
    p.add_argument("--emit-providers", default=None, help="write the 3 tiers out as a providers.yaml snippet")
    args = p.parse_args(argv)

    print("[nishiki models] fetching OpenRouter /models live… (no key, no charges)")
    catalog = models_cmd.fetch_catalog()
    tiers = models_cmd.suggest(catalog, vision=args.vision, search=args.search,
                               include_free=args.include_free)
    print(f"  candidate pool {tiers.get('n_pool', 0)} (narrowed from {len(catalog)} total)\n")
    for tier, jp in (("high", "high-performance"), ("mid", "mid-tier"), ("cost", "cost-efficient")):
        print(f"=== {jp} ===")
        for m in tiers.get(tier, []):
            print(f"  {m['id']:48} in {m['in_per_1k']:.4f} / out {m['out_per_1k']:.4f} USD/1k"
                  + ("  [vision]" if m["vision"] else ""))
        print()
    print("→ Final choice is the human/orchestrator (the winner is decided by real measurements).")

    if args.emit_providers:
        picked = tiers.get("high", [])[:1] + tiers.get("mid", [])[:1] + tiers.get("cost", [])[:1]
        with open(args.emit_providers, "w", encoding="utf-8") as f:
            f.write(models_cmd.to_providers_yaml(picked))
        print(f"\nproviders snippet → {args.emit_providers} (1 each: high/mid/cost-efficient)")






_SCORER_JP = {
    "builtin:label_accuracy": "accuracy (exact match)",
    "builtin:macro_f1": "macro-F1 (robust to class imbalance)",
}












def _experiment_context(exp_dir):
    """Assemble context from the experiment directory (pipeline/NOTES) + one example jsonl row."""
    import json
    ctx = []
    for fn in ("pipeline.yaml", "NOTES.md"):
        p = os.path.join(exp_dir, fn)
        if os.path.exists(p):
            ctx.append(f"## {fn}\n" + open(p, encoding="utf-8").read())
    try:
        import yaml
        pipe = yaml.safe_load(open(os.path.join(exp_dir, "pipeline.yaml"), encoding="utf-8"))
        inputs = []
        for s in pipe.get("steps", []):
            for inp in s.get("inputs", []):
                if isinstance(inp, str) and inp.startswith("input."):
                    f = inp[len("input."):]
                    if f not in inputs:
                        inputs.append(f)
        gold = pipe["eval"]["kpi"].get("gold", "expected.total")
        leaf = gold.split(".", 1)[1] if "." in gold else gold
        example = {"input": {k: "<…>" for k in inputs} or {"attachment": "data/files/0001.jpg"},
                   "expected": {leaf: "<gold value>"}}
        ctx.append("## Shape of one data row (samples.jsonl)\n" + json.dumps(example, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        pass
    return "\n\n".join(ctx)


def _cmd_ask(argv):
    """nishiki ask — ask the orchestrator brain a question grounded in your own config (interactive or one-shot)."""
    from . import orchestrator
    p = argparse.ArgumentParser(prog="nishiki ask")
    p.add_argument("question", nargs="*", help="question (omit for interactive mode)")
    p.add_argument("--experiment", default="exp", help="experiment directory to use as context")
    args = p.parse_args(argv)

    if not orchestrator.configured_brain():
        print("No brain selected. Run `nishiki setup` first.")
        return
    ctx = _experiment_context(args.experiment) if os.path.isdir(args.experiment) else ""
    preamble = (
        "You are the Nishiki (KOI optimizer) assistant. Below is the user's experiment config. "
        "Answer the question grounded in this config, **clearly and specifically, in the user's own language** "
        "(detect it from the question and reply in that language). "
        "If asked about the data (jsonl), show a concrete example using the 'shape of one row' above.\n\n"
        + (ctx + "\n\n" if ctx else "(No experiment config. Answer in general terms.)\n\n")
    )

    def answer(q):
        print("\n…thinking…")
        try:
            print("\n" + orchestrator.call(preamble + "# Question\n" + q).strip() + "\n")
        except Exception as e:  # noqa: BLE001
            print(f"\n(brain call failed: {e})\n")

    if args.question:
        answer(" ".join(args.question))
        return
    print("You can ask Nishiki a question (empty Enter or q to quit).")
    if ctx:
        print(f"  * context: answers will draw on the pipeline and NOTES in {args.experiment}/.")
    while True:
        q = _ask("\nAsk away ▶  ")
        if not q or q.lower() == "q":
            break
        answer(q)


def _cmd_start(argv):
    """Production flow entry point: pick the orchestrator, launch it with the skill → from there the AI asks questions and drives."""
    from . import orchestrator
    p = argparse.ArgumentParser(prog="nishiki start")
    p.add_argument("--target", default=None, help="target project (the AI asks if omitted)")
    args = p.parse_args(argv)

    cfg = orchestrator.load_config()
    brain = cfg.get("brain")
    if not brain:
        brains = orchestrator.available_brains()
        print("Choose the orchestrator (from here on it drives by asking you questions):\n")
        for i, (name, ok, dep) in enumerate(brains, 1):
            print(f"   {i}) {name:11} [{'✓ available' if ok else '— not found:'+dep}]")
        sel = _ask("\n   number [1]: ", "1")
        try:
            brain = brains[int(sel) - 1][0]
        except (ValueError, IndexError):
            brain = "claude_p"
        cfg["brain"] = brain
        orchestrator.save_config(cfg)

    skill_path = os.path.join(os.path.dirname(__file__), "playbooks", "koi.md")
    if not os.path.exists(skill_path):
        print(f"playbook not found: {skill_path}")
        return
    skill = open(skill_path, encoding="utf-8").read()
    role = (
        "You are the Nishiki orchestrator.\n"
        "[LANGUAGE] Detect the language of the user's first message and conduct the ENTIRE session "
        "in that language (English → English, Japanese → Japanese, etc.). The playbook below is written "
        "in English; when the user's language differs, translate everything user-facing — your questions, "
        "explanations, and the prose you author into generated files (AGENT.md, KOI.yaml comments) — into "
        "the user's language.\n\n"
        "You are the Nishiki orchestrator. Follow the playbook below strictly, "
        "asking the user one question at a time (target → source → KPI confirmation → billing GO), "
        "and run the nishiki commands YOURSELF to drive all the way to the KOI optimization table. "
        "Do not make the human type the CLI one command at a time. Stay strictly read-only; for any charges, always present the amount → require an explicit GO.\n\n"
        "=== Nishiki playbook (koi.md) ===\n" + skill
    )
    kickoff = ("Nishiki KOI measurement session. Reply to the user in THEIR language (detect it). "
               + (f"Target is {args.target}." if args.target
                  else "First, ask the user for the path of the project to measure."))

    import shutil
    cli = {"claude_p": "claude", "codex": "codex"}.get(brain)
    if cli and shutil.which(cli):
        if brain == "claude_p":
            print("→ Launching claude (orchestrator). From here the AI asks the questions.\n")
            os.execvp("claude", ["claude", "--append-system-prompt", role, kickoff])
        else:
            print("→ Launching codex (orchestrator).\n")
            os.execvp("codex", ["codex", role + "\n\n" + kickoff])
    else:
        if cli:
            print(f"The '{cli}' CLI for brain[{brain}] was not found on PATH. "
                  f"Install it, or pick another brain with `nishiki config --brain ...`.")
        else:
            print(f"brain[{brain}] doesn't support automated interactive launch. "
                  f"Load the skill below and start it manually:")
        print(f"  skill: {skill_path}")
        print(f"  instruction: {kickoff}")


def _cmd_calibrate_env(argv):
    """Generated profile (MODELS.yaml/KOI.yaml) → emit NZ_* env for the container glue.

    Used in the skill's measurement step: drop only priced candidates into NZ_CATALOG (a JSON file),
    the candidate keys into NZ_CANDIDATES, and the gold batch into NZ_GOLD_BATCH (profile-driven).
    """
    import json
    import yaml
    p = argparse.ArgumentParser(prog="nishiki calibrate-env")
    p.add_argument("--experiment", required=True, help="profile area created by init")
    p.add_argument("--catalog-out", default="/tmp/nz_catalog.json", help="write destination for NZ_CATALOG")
    args = p.parse_args(argv)

    koi = yaml.safe_load(open(os.path.join(args.experiment, "KOI.yaml"), encoding="utf-8"))
    models = yaml.safe_load(open(os.path.join(args.experiment, "MODELS.yaml"), encoding="utf-8"))["models"]
    cands = koi.get("candidates")
    if not isinstance(cands, list):           # ALL etc. → all models
        cands = list(models)
    inj, keys, skipped = {}, [], []
    for k in cands:
        m = models.get(k)
        if not m or m.get("in") is None:
            skipped.append(k); continue
        inj[k] = {"model_id": m["model_id"], "in": m["in"], "out": m["out"],
                  "quality_hint": m.get("quality_hint", "unknown"), "note": m.get("note", ""),
                  "call": m.get("call", "on_demand")}  # the glue distinguishes Bedrock/OpenRouter
        keys.append(k)
    # Always include reference (current = CASCADE etc.) first = to measure the baseline for the current-model ratio (multiplier).
    # The glue special-cases CASCADE (the target's current *_CASCADE), so it needs no NZ_CATALOG.
    ref = koi.get("reference")
    if ref and ref not in keys:
        keys.insert(0, ref)
    with open(args.catalog_out, "w", encoding="utf-8") as f:
        json.dump(inj, f, ensure_ascii=False)
    batch = (koi.get("gold") or {}).get("batch_id")
    print(f"# NZ_CATALOG → {args.catalog_out} ({len(inj)} candidates)")
    if ref:
        print(f"# reference (baseline for the current-model ratio) = {ref} included in candidates (for computing the 1.0x current ratio)")
    if skipped:
        print(f"# skipped (unpriced): {', '.join(skipped)}")
    # If there are cross-border (openai_compatible = OpenRouter etc.) candidates, spell out the key + residency-approval prerequisite.
    crossborder = [k for k, v in inj.items() if v["call"] == "openai_compatible"]
    if crossborder:
        print(f"# ⚠ cross-border candidates (via OpenRouter) = {', '.join(crossborder)}: "
              f"a real run requires OPENROUTER_API_KEY and 'approval to route outside the data residency'")
    print(f"NZ_CANDIDATES={','.join(keys)}")
    print(f"NZ_GOLD_BATCH={batch if isinstance(batch, int) else 'TBD (confirm with the user)'}")
    print(f"NZ_CATALOG_FILE={args.catalog_out}")
    # remember the models + gold batch for the glue (Path A) run, so next time we can re-measure the
    # SAME models without reconfiguring (the orchestrator reads last_run.json in Step 1.5).
    _write_last_run(args.experiment, models=keys,
                    gold_batch=batch if isinstance(batch, int) else None,
                    reference=ref, via="calibrate-env")


def _catalog_from_models(models, keys):
    """MODELS.yaml models dict + candidate keys → catalog for GenericAdapter (exclude entries missing a price)."""
    cat = {}
    for k in keys:
        m = models.get(k)
        if not m or m.get("in") is None:
            continue
        cat[k] = {"model_id": m["model_id"], "in": m["in"], "out": m["out"],
                  "call": m.get("call", "openai_compatible"), "note": m.get("note", "")}
    return cat


def _cmd_measure(argv):
    """Generic run: measure with KOI.yaml (generic adapter config) + MODELS.yaml, no target-specific glue.

    Runs the KOI.yaml authored by init (task_type / scorer.kpi / gold / run.{prompt,parser,gold_format} /
    candidates) through GenericAdapter → save run JSON (probe=cost / run=scoring).
    """
    import json
    import time
    import yaml
    from . import generic_adapter, runner
    p = argparse.ArgumentParser(prog="nishiki measure")
    p.add_argument("--experiment", required=True, help="profile area (KOI.yaml/MODELS.yaml)")
    p.add_argument("--mode", default="probe", choices=["probe", "run"], help="probe=cost / run=scoring")
    p.add_argument("--limit", type=int, default=0, help="cap on number of items (0=all)")
    p.add_argument("--probe-n", type=int, default=3, help="number of items to run per candidate in probe")
    p.add_argument("--candidates", default=None, help="candidate keys (comma-separated; default=KOI candidates)")
    p.add_argument("--gold-data", default=None, help="override the gold data path (when KOI gold.data is TBD)")
    p.add_argument("--dry-run", action="store_true",
                   help="don't call real models (zero cost); just verify the wiring. Check that load→prompt→parse→scoring passes with stub responses")
    args = p.parse_args(argv)
    from . import dotenv
    dotenv.autoload(args.experiment)   # also pick up a .env in the experiment/profile dir

    koi = yaml.safe_load(open(os.path.join(args.experiment, "KOI.yaml"), encoding="utf-8"))
    models = yaml.safe_load(open(os.path.join(args.experiment, "MODELS.yaml"), encoding="utf-8"))["models"]
    cands = (args.candidates.split(",") if args.candidates
             else (koi.get("candidates") if isinstance(koi.get("candidates"), list) else list(models)))
    catalog = _catalog_from_models(models, cands)
    keys = [k for k in cands if k in catalog]
    if not keys:
        print("No valid candidates (none are priced). Check MODELS.yaml/--candidates.")
        return
    gold_data = args.gold_data or (koi.get("gold") or {}).get("data")
    if not gold_data or gold_data == "TBD":
        print("Gold data not set (KOI.yaml gold.data is TBD). Specify --gold-data <path>.")
        return

    # --dry-run: stub backend that calls no real models (zero cost). Verify the wiring (load→prompt→parse→scoring) only.
    stub = None
    if args.dry_run:
        def stub(model_id, prompt, max_tokens=1024):
            return {"text": "NONE", "input_tokens": 0, "output_tokens": 0}
    adapter = generic_adapter.GenericAdapter.from_koi(
        koi, catalog, gold_data=gold_data, limit=args.limit, call=stub)
    print(f"[measure{' DRY' if args.dry_run else ''}] task_type={koi.get('task_type')} "
          f"scorer={koi.get('scorer',{}).get('kpi')} candidates={len(keys)} "
          f"items={len(adapter.load_items())} mode={args.mode}")
    if args.mode == "probe":
        out = runner.mode_probe(adapter, keys, args.probe_n)
    else:
        out = runner.mode_run(adapter, keys)
    runs_dir = os.path.join(args.experiment, "runs")
    os.makedirs(runs_dir, exist_ok=True)
    stamp = int(time.time())
    path = os.path.join(runs_dir, f"{stamp}_{args.mode}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    # remember what was measured so next time we can re-measure the SAME models without reconfiguring
    winner = (out["candidates"][0]["label"]
              if args.mode == "run" and out.get("candidates") else None)
    _write_last_run(args.experiment, models=keys, mode=args.mode, gold_data=gold_data,
                    limit=args.limit, probe_n=args.probe_n, winner=winner,
                    dry_run=args.dry_run or None, via="measure", result=path)
    print(f"→ {path}")
    if args.mode == "run":
        print("  next: nishiki koi-report --experiment", args.experiment)
    else:
        print(f"  probe actual spend ${out.get('probe_spend_total', 0):.4f} / "
              f"full-run projection ${out.get('full_run_total_est', 0):.2f}")


def _cmd_estimate(argv):
    """Instant KOI estimate for a runtime prompt/input — NO model call (edge estimate from past runs)."""
    from . import koi_estimate
    p = argparse.ArgumentParser(prog="nishiki estimate")
    p.add_argument("--experiment", required=True, help="profile area (.nishiki with a past scored run)")
    p.add_argument("--model", default=None, help="estimate one model (default = every measured model)")
    p.add_argument("--prompt", default=None, help="the runtime prompt text")
    p.add_argument("--prompt-file", default=None, help="read the prompt text from a file")
    p.add_argument("--image", default=None, help="an input image (PNG/JPEG); input tokens estimated from its size")
    p.add_argument("--in-tokens", type=int, default=None, help="override input tokens")
    p.add_argument("--out-tokens", type=int, default=None, help="override output tokens (default 256)")
    args = p.parse_args(argv)

    prompt = args.prompt
    if args.prompt_file:
        with open(args.prompt_file, encoding="utf-8") as f:
            prompt = f.read()
    res = koi_estimate.estimate(args.experiment, model=args.model, prompt=prompt, image=args.image,
                                in_tokens=args.in_tokens, out_tokens=args.out_tokens)
    if not res["rows"]:
        print("No measured run to estimate from. Run a scored run (`nishiki measure --mode run`) first.")
        return
    src = res["rows"][0]["cost_src"]
    print(f"[estimate] kpi={res['kpi_name']} basis={os.path.basename(res['run_path'] or '?')} "
          f"({src}); no model called")
    if res.get("image_dims"):
        print(f"  image {res['image_dims'][0]}x{res['image_dims'][1]} ≈ {res['image_tokens']} input tokens")
    hdr = f"  {'model':22} {'KPI':>8} {'$/item':>11} {'KOI':>12}"
    if res["reference"]:
        hdr += f" {'vs ' + res['reference']:>11}"
    print(hdr)
    for r in sorted(res["rows"], key=lambda x: (x["koi"] is None, -(x["koi"] or 0))):
        kpi = "-" if r["kpi"] is None else f"{r['kpi']:.3f}"
        cost = "-" if r["cost_per_item"] is None else f"${r['cost_per_item']:.5f}"
        koi = "-" if r["koi"] is None else f"{r['koi']:.2f}"
        line = f"  {r['model']:22} {kpi:>8} {cost:>11} {koi:>12}"
        if res["reference"]:
            vr = r.get("vs_reference")
            line += f" {('-' if vr is None else f'{vr:.1f}x'):>11}"
        print(line)
    print("  (KPI reused from the last run; cost from local tokens × price. Estimate only — run a real "
          "measure to confirm.)")


def _hud_loop(experiment, log, interval, keep, *, waiting_hint=""):
    """Tail `log` and redraw the live-KOI HUD in place until Ctrl-C. Shared by watch and run --watch."""
    import time
    from . import live, koi_estimate
    basis = koi_estimate.load_basis(experiment)
    if not basis.get("measured"):
        print("No measured run yet — do a scored run first so the estimate has a basis (KPI/cost).")
        return
    prev_n, last = -1, None
    print("\033[2J", end="")                                      # clear once
    try:
        while True:
            lines = open(log, encoding="utf-8").read().splitlines() if os.path.exists(log) else []
            events = [ev for ln in lines if (ev := live.parse_event(ln))]
            if len(events) != prev_n:                             # re-estimate only when new events land
                prev_n = len(events)
                if events:
                    window = [(ev, live.estimate_event(experiment, ev, basis=basis))
                              for ev in events[-keep:]]
                    res = window[-1][1]
                    route = koi_estimate.resolve_model(basis, window[-1][0].get("model"))
                    history = []
                    for ev, r in window:
                        key = koi_estimate.resolve_model(basis, ev.get("model"))
                        cur = next((x for x in r["rows"] if x["model"] == key), None)
                        history.append((cur["cost_per_item"] if cur else None,
                                        cur["koi"] if cur else None))
                    last = (res, route, live.rolling_stats(history))
            now = time.strftime("%H:%M:%S")
            if last:
                res, route, stats = last
                frame = live.render_frame(res, route, stats, now_str=now)
            else:
                frame = (f"┌ Nishiki live KOI — waiting for events ┐\n"
                         f"  {waiting_hint or ('append a line to ' + log)}\n"
                         f"  (Ctrl-C to quit)        {now}")
            print("\033[H" + frame + "\033[J", end="", flush=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nstopped.")


def _cmd_watch(argv):
    """Live KOI HUD: tail <experiment>/live.jsonl and redraw an in-place terminal view (no model calls)."""
    p = argparse.ArgumentParser(prog="nishiki watch")
    p.add_argument("--experiment", required=True, help="profile area (.nishiki with a past scored run)")
    p.add_argument("--log", default=None, help="event log to tail (default <experiment>/live.jsonl)")
    p.add_argument("--interval", type=float, default=0.5, help="poll/redraw interval (seconds)")
    p.add_argument("--keep", type=int, default=20, help="rolling-stats window size")
    args = p.parse_args(argv)
    log = args.log or os.path.join(args.experiment, "live.jsonl")
    _hud_loop(args.experiment, log, args.interval, args.keep)


def _cmd_relay(argv):
    """B2 relay: a local proxy for an OpenAI-compatible (OpenRouter) endpoint — logs prompts, forwards calls."""
    from . import relay
    p = argparse.ArgumentParser(prog="nishiki relay")
    p.add_argument("--experiment", required=True, help="profile area (writes events to <DIR>/live.jsonl)")
    p.add_argument("--port", type=int, default=8900, help="local port to listen on (default 8900)")
    p.add_argument("--upstream", default="https://openrouter.ai/api/v1",
                   help="the real OpenAI-compatible endpoint to forward to (includes /api/v1)")
    args = p.parse_args(argv)

    server = relay.make_server(args.experiment, args.port, upstream=args.upstream)
    base = f"http://127.0.0.1:{args.port}"
    print(f"[relay] {base}  →  {args.upstream}    (no extra model calls)")
    print(f"        events → {server.nishiki_log}")
    print("  Point your agent's OpenAI-compatible base URL at the relay ROOT (no source edit), e.g.:")
    print(f"    export NZ_OPENAI_BASE={base}        # nishiki / its glue")
    print(f"    export OPENROUTER_BASE_URL={base}   # or your agent's equivalent base-URL env")
    print(f"  Then in another terminal: nishiki watch --experiment {args.experiment}")
    print("  Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        server.server_close()


def _cmd_run(argv):
    """Run your agent with a runtime probe around its model-call (the choke) → logs usage to live.jsonl.

    Wraps the choke (KOI.yaml injection.choke, or --choke module:func) WITHOUT editing the agent's
    source; the wrapper lives in nishiki and writes events to <experiment>/live.jsonl. Provider-agnostic
    (Bedrock / OpenRouter / raw API). The agent must be launched as a Python process for the probe to load.
    """
    import subprocess
    import tempfile
    p = argparse.ArgumentParser(prog="nishiki run")
    p.add_argument("--experiment", required=True, help="profile area (events → <DIR>/live.jsonl)")
    p.add_argument("--choke", default=None, help="module:func to wrap (default: KOI.yaml injection.choke)")
    p.add_argument("--probe", default=None,
                   help="custom adapter 'module:func' mapping (args, kwargs, result) → event (for exact usage)")
    p.add_argument("--log", default=None, help="event log (default <experiment>/live.jsonl)")
    p.add_argument("--watch", action="store_true",
                   help="also show the live KOI HUD in THIS terminal (agent runs in the background)")
    p.add_argument("--fresh", action="store_true", help="truncate live.jsonl before starting")
    p.add_argument("cmd", nargs=argparse.REMAINDER, help="-- the command that launches your agent")
    args = p.parse_args(argv)

    choke = args.choke
    if not choke:
        import yaml
        koi_p = os.path.join(args.experiment, "KOI.yaml")
        koi = yaml.safe_load(open(koi_p, encoding="utf-8")) if os.path.exists(koi_p) else {}
        choke = (koi.get("injection") or {}).get("choke")
    if not choke or ":" not in choke:
        print("No choke to wrap. Set --choke module:func, or KOI.yaml injection.choke (e.g. pkg.mod:func).")
        return 2
    cmd = args.cmd[1:] if args.cmd and args.cmd[0] == "--" else args.cmd
    if not cmd:
        print("Nothing to run. Usage: nishiki run --experiment DIR -- <command that starts your agent>")
        return 2

    log = os.path.abspath(args.log or os.path.join(args.experiment, "live.jsonl"))
    inject = tempfile.mkdtemp(prefix="nz_probe_")               # a sitecustomize that loads the probe
    with open(os.path.join(inject, "sitecustomize.py"), "w", encoding="utf-8") as f:
        f.write("import nishiki.autoprobe as _a; _a.install()\n")
    env = dict(os.environ)
    env["PYTHONPATH"] = inject + os.pathsep + env.get("PYTHONPATH", "")
    env["NZ_PROBE_CHOKE"], env["NZ_PROBE_LOG"] = choke, log
    if args.probe:
        env["NZ_PROBE_ADAPTER"] = args.probe
    if args.fresh or args.watch:
        open(log, "w", encoding="utf-8").close()                 # start the HUD from a clean log

    if not args.watch:
        print(f"[run] probe wraps {choke} → {log}   (no source edit; provider-agnostic)")
        print(f"      live HUD (other terminal): nishiki watch --experiment {args.experiment}")
        print(f"      launching: {' '.join(cmd)}\n")
        try:
            return subprocess.call(cmd, env=env)
        except FileNotFoundError:
            print(f"command not found: {cmd[0]}")
            return 127

    # --watch: agent runs in the background (logs → <experiment>/run.log); HUD in this terminal.
    runlog = os.path.join(args.experiment, "run.log")
    print(f"[run] probe wraps {choke}; agent output → {runlog}; starting HUD…  (Ctrl-C stops both)")
    try:
        proc = subprocess.Popen(cmd, env=env,
                                stdout=open(runlog, "w", encoding="utf-8"),
                                stderr=subprocess.STDOUT)
    except FileNotFoundError:
        print(f"command not found: {cmd[0]}")
        return 127
    try:
        _hud_loop(args.experiment, log, 0.5, 20,
                  waiting_hint=f"agent running (pid {proc.pid}); trigger a call. logs → {runlog}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    return proc.returncode or 0


def _cmd_live(argv):
    """Show the live KOI HUD (no model calls). The short, friendly entry for watching KOI in real time.

    Something feeds <experiment>/live.jsonl — the container probe overlay, `nishiki relay`, or
    `nishiki run` in another terminal. To launch a host agent + HUD in one go, use `nishiki run --watch`.
    --experiment defaults to ./.nishiki.
    """
    p = argparse.ArgumentParser(prog="nishiki live")
    p.add_argument("--experiment", default=None, help="profile area (default: ./.nishiki)")
    p.add_argument("--log", default=None, help="event log to tail (default <experiment>/live.jsonl)")
    p.add_argument("--interval", type=float, default=0.5, help="HUD redraw interval (seconds)")
    p.add_argument("--keep", type=int, default=20, help="rolling-stats window size")
    p.add_argument("--web", action="store_true",
                   help="open a rich browser dashboard (cumulative cost / avg KOI / charts) instead of the terminal HUD")
    p.add_argument("--port", type=int, default=8765, help="port for --web (default 8765)")
    p.add_argument("--no-open", action="store_true", help="with --web, don't auto-open the browser")
    args = p.parse_args(argv)

    exp = args.experiment or os.path.join(os.getcwd(), ".nishiki")
    if not os.path.exists(os.path.join(exp, "KOI.yaml")):
        print(f"No Nishiki profile at {exp}. Run `nishiki start` first, or pass --experiment <DIR>.")
        return 2
    log = args.log or os.path.join(exp, "live.jsonl")
    if args.web:
        from . import webui
        # Foreground serve for a human (a TTY) or when we ARE the detached child (NZ_DASH_SERVE=1).
        if os.environ.get("NZ_DASH_SERVE") == "1" or sys.stdout.isatty():
            webui.serve(exp, log, port=args.port, interval=args.interval, open_browser=not args.no_open)
            return
        # Non-interactive caller (e.g. the orchestrator): self-detach so we never block it, and ALWAYS
        # print the deep-link URLs to stdout — deterministic, surfaced no matter who launched us.
        url, pid = _launch_web_dashboard(exp, port=args.port, open_browser=not args.no_open, log=log)
        _print_live_next_step(exp, launched_url=url, pid=pid)
        return
    _hud_loop(exp, log, args.interval, args.keep)


def _cmd_history(argv):
    """Per-call KOI history: every logged call (each image/prompt) and what its KOI was."""
    import json
    import time
    from . import live, koi_estimate
    p = argparse.ArgumentParser(prog="nishiki history")
    p.add_argument("--experiment", default=None, help="profile area (default: ./.nishiki)")
    p.add_argument("--log", default=None, help="event log (default <experiment>/live.jsonl)")
    p.add_argument("--last", type=int, default=0, help="show only the last N calls (0 = all)")
    args = p.parse_args(argv)

    exp = args.experiment or os.path.join(os.getcwd(), ".nishiki")
    if not os.path.exists(os.path.join(exp, "KOI.yaml")):
        print(f"No Nishiki profile at {exp}. Run `nishiki start` first, or pass --experiment <DIR>.")
        return 2
    log = args.log or os.path.join(exp, "live.jsonl")
    lines = open(log, encoding="utf-8").read().splitlines() if os.path.exists(log) else []
    evs = []
    for ln in lines:
        try:
            o = json.loads(ln)
        except ValueError:
            continue
        if isinstance(o, dict) and (o.get("model") is not None):
            evs.append(o)
    if not evs:
        print(f"No calls logged yet ({log}). Run your agent (with the probe) and process something first.")
        return
    if args.last:
        evs = evs[-args.last:]

    basis = koi_estimate.load_basis(exp)
    ref = basis.get("reference")
    print(f"  {'#':>3}  {'time':8}  {'model':18}{'in/out':>12}{'$/item':>11}{'KOI':>10}"
          + (f"  {'vs ' + ref:>10}" if ref else ""))
    kois = []
    for i, ev in enumerate(evs, 1):
        res = live.estimate_event(exp, ev, basis=basis)
        route = koi_estimate.resolve_model(basis, ev.get("model"))
        r = next((x for x in res["rows"] if x["model"] == route), None)
        t = time.strftime("%H:%M:%S", time.localtime(ev["ts"])) if ev.get("ts") else "-"
        io = f"{ev.get('in_tokens', '?')}/{ev.get('out_tokens', '?')}"
        cost = live._money(r["cost_per_item"]) if r else "-"
        koi = live._koi(r["koi"]) if r and r["koi"] is not None else "-"
        line = f"  {i:>3}  {t:8}  {(route or '?'):18}{io:>12}{cost:>11}{koi:>10}"
        if ref:
            vr = r.get("vs_reference") if r else None
            line += f"  {('-' if vr is None else f'{vr:.1f}x'):>10}"
        print(line)
        if r and r["koi"] is not None:
            kois.append(r["koi"])
    if kois:
        print(f"  — {len(evs)} calls · avg KOI {live._koi(sum(kois) / len(kois))}")


def _launch_web_dashboard(experiment, port=8765, open_browser=True, log=None):
    """Start `nishiki live --web` as a detached background server and return (url, pid|None).

    Deterministic UI launch: spawned in code (not left to the orchestrator) so the dashboard always
    comes up after a report. Non-blocking — the server runs in its own session; the parent returns at
    once. The child gets NZ_DASH_SERVE=1 so it serves in the foreground instead of re-detaching (no
    recursion). Logs go to <experiment>/live_web.log. Returns (url, None) if the spawn failed.
    """
    import subprocess
    url = f"http://127.0.0.1:{port}"
    cmd = [sys.executable, "-m", "nishiki", "live", "--web",
           "--experiment", experiment, "--port", str(port)]
    if log:
        cmd += ["--log", log]
    if not open_browser:
        cmd.append("--no-open")
    env = dict(os.environ, NZ_DASH_SERVE="1")         # child serves in foreground; does not re-detach
    try:
        logf = open(os.path.join(experiment, "live_web.log"), "ab")
        kw = {"stdout": logf, "stderr": logf, "stdin": subprocess.DEVNULL, "env": env}
        if hasattr(os, "setsid"):
            kw["start_new_session"] = True            # detach: survives this process, no controlling tty
        proc = subprocess.Popen(cmd, **kw)
        return url, proc.pid
    except Exception as e:  # noqa: BLE001
        print(f"(could not auto-launch the web dashboard: {e})")
        return url, None


def _print_live_next_step(experiment, launched_url=None, pid=None):
    """Deterministic, impossible-to-miss pointer to the LIVE dashboard (printed in code, every time).

    The dashboard is the ONE place to look — never mention a static HTML file here.
    """
    print("=" * 64)
    if launched_url:
        print(f"📈 KOI dashboard — open: {launched_url}"
              + (f"   [serving in background, PID {pid}]" if pid else ""))
        print(f"   • first measurement : {launched_url}/#measured")
        print(f"   • Live (per call)   : {launched_url}/#live")
        print("   one screen, two tabs · no model calls")
        if pid:
            print(f"   stop it later with:  kill {pid}")
    else:
        print(f"📈 KOI dashboard:  nishiki live --web --experiment {experiment}")
        print("   then: first measurement → /#measured · Live (per call) → /#live")
    print("=" * 64)


def _cmd_koi_report(argv):
    """Glue scored-run JSON + floors from KOI.yaml → generate the KOI optimization table HTML."""
    from . import koi_report
    p = argparse.ArgumentParser(prog="nishiki koi-report")
    p.add_argument("--experiment", required=True, help="profile area (where KOI.yaml lives)")
    p.add_argument("--result", default=None,
                   help="glue scored-run JSON. If omitted, uses the latest in <experiment>/runs/")
    p.add_argument("--out", default=None, help="output HTML (default <experiment>/koi_report.html)")
    p.add_argument("--web", action="store_true",
                   help="also launch the live KOI dashboard (background browser view) after the report")
    p.add_argument("--port", type=int, default=8765, help="port for --web (default 8765)")
    p.add_argument("--no-open", action="store_true", help="with --web, don't auto-open the browser")
    args = p.parse_args(argv)

    result = args.result
    if not result:
        runs = os.path.join(args.experiment, "runs")
        cands = sorted(
            (os.path.join(runs, f) for f in os.listdir(runs)) if os.path.isdir(runs) else [],
            key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0)
        if not cands:
            print(f"No result JSON. Specify --result or save one in {runs}/.")
            return
        result = cands[-1]
    koi_yaml = os.path.join(args.experiment, "KOI.yaml")
    out = args.out or os.path.join(args.experiment, "koi_report.html")
    path, best = koi_report.generate(result, koi_yaml, out)            # html persisted as an artifact (not advertised)
    rec = best or "none = all candidates failed to pass the cutoff"
    # The live dashboard is the ONE UI — surface it deterministically (in code); never advertise the static HTML.
    if args.web:
        url, pid = _launch_web_dashboard(args.experiment, port=args.port, open_browser=not args.no_open)
        print(f"KOI dashboard ready  (recommended: {rec})")
        _print_live_next_step(args.experiment, launched_url=url, pid=pid)
    else:
        print(f"KOI ranking computed  (recommended: {rec})")
        _print_live_next_step(args.experiment)


def _cmd_adapter_path(argv):
    """Print the absolute path of the target-specific glue `adapters/<name>/calibrate.py` (makes it cwd-independent).

    The glue lives outside the package (a stdin-run script on the container side), so a relative path would be cwd-dependent.
    The orchestrator can use it as `python - < "$(nishiki adapter-path example_target)"` and resolve it from anywhere.
    """
    p = argparse.ArgumentParser(prog="nishiki adapter-path")
    p.add_argument("name", help="adapter name (e.g. example_target)")
    args = p.parse_args(argv)
    # src-layout (editable): __file__=<proj>/src/nishiki/__main__.py → <proj>/adapters/<name>/calibrate.py
    project = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(project, "adapters", args.name, "calibrate.py")
    if not os.path.exists(path):
        print(f"adapter not found: {path}", file=sys.stderr)
        sys.exit(1)
    print(path)


def _latest_run(experiment):
    """Return the path of the latest **scored run** (`*_run.json`) in <experiment>/runs/ (None if absent).

    A probe (`*_probe.json`) is cost-only and has no floors scoring fields → unusable for failure-band computation, so it's excluded.
    """
    runs = os.path.join(experiment, "runs")
    cands = sorted(
        (os.path.join(runs, f) for f in os.listdir(runs) if f.endswith("_run.json"))
        if os.path.isdir(runs) else [],
        key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0)
    return cands[-1] if cands else None


def _write_last_run(experiment, **fields):
    """Persist `<experiment>/last_run.json` = what was measured last time, so the next session can
    re-measure with the SAME models without reconfiguring (the orchestrator reads it in Step 1.5).

    Shared shape (keys optional per path): models[], mode, gold_batch, gold_data, limit, winner,
    via ('measure'|'calibrate-env'), result (run JSON path).
    """
    import json
    rec = {k: v for k, v in fields.items() if v is not None}
    try:
        with open(os.path.join(experiment, "last_run.json"), "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _read_last_run(experiment):
    """Read `<experiment>/last_run.json` (None if absent/unreadable)."""
    import json
    p = os.path.join(experiment, "last_run.json")
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _cmd_suggest_floor(argv):
    """Deterministically derive the failure-band floor (--min-price suggestion) from past-run measurements (design doc §18.9).

    No AI quality guessing: computed only from the prices of models that fell below the floors. The orchestrator handles the proposal text and approval.
    """
    import json
    import yaml
    from . import suggest_floor, init_cmd
    p = argparse.ArgumentParser(prog="nishiki suggest-floor")
    p.add_argument("--experiment", required=True, help="profile area (where KOI.yaml/MODELS.yaml live)")
    p.add_argument("--result", default=None, help="scored-run JSON. If omitted, the latest in <experiment>/runs/")
    args = p.parse_args(argv)

    result = args.result or _latest_run(args.experiment)
    if not result:
        print(f"No past run (*_run.json in {args.experiment}/runs/). Do a calibrate scored run first.")
        return
    run = json.load(open(result, encoding="utf-8"))
    koi_path = os.path.join(args.experiment, "KOI.yaml")
    koi = yaml.safe_load(open(koi_path, encoding="utf-8")) if os.path.exists(koi_path) else {}
    floors = (koi or {}).get("floors") or {}
    prices = {m["key"]: m["in_mtok"]
              for m in init_cmd.load_models_yaml(os.path.join(args.experiment, "MODELS.yaml"))}
    r = suggest_floor.compute_floor(run, floors, prices)
    print(f"# failure-band floor computation (basis: {os.path.basename(result)})")
    print(suggest_floor.format_text(r))
    if r["suggested_min_price"] is not None:
        # In a form an AI/script can pick up (pass straight to --min-price)
        print(f"MIN_PRICE={r['suggested_min_price']:g}")


def _help():
    print("""nishiki — the KOI optimizer

Production flow (this is all you need to remember):
  nishiki start                pick the orchestrator → from there the AI asks questions and drives to the KOI table
  nishiki ask ["question"]      ask a question grounded in your own config (interactive)
  nishiki live [--web]         watch KOI update in real time as your agent runs (--web = rich browser dashboard)
  nishiki history              per-call KOI history — what each processed item scored

Individual commands (the AI uses these internally; you rarely run them by hand):
  nishiki setup                first-time setup of brain/candidate source/residency policy
  nishiki models [--vision]    fetch latest models + prices live (no key)
  nishiki init --target DIR --source bedrock[,openrouter] [--add]   auto-generate config / add source
  nishiki measure --experiment DIR [--mode probe|run]   measure with the generic adapter (no target-specific glue)
  nishiki calibrate-env --experiment DIR   assemble the measurement env (NZ_*) (for container-specific glue)
  nishiki suggest-floor --experiment DIR   deterministically suggest a failure-band floor from a past run
  nishiki estimate --experiment DIR [--prompt … | --image …]   instant KOI estimate, no model call (edge)
  nishiki watch --experiment DIR           live KOI HUD: tail live.jsonl, redraw in place (no model calls)
  nishiki relay --experiment DIR [--port]  local proxy: log prompts → live.jsonl, forward calls (feeds watch)
  nishiki run --experiment DIR -- CMD…     run your agent with a probe on the choke → live.jsonl (no source edit)
  nishiki koi-report --experiment DIR [--web]  KOI table HTML (+ --web also launches the live dashboard)
""")


def _smart_entry():
    """Bare `nishiki` = just launch the orchestrator AI. It decides the rest:
    first time (no profile) → pick the brain + ask the project path + run the full flow;
    next time (a profile exists here) → it offers to re-measure the SAME models (Step 1.5).

    A human can still type `nishiki start` explicitly. When not interactive (no TTY) or the brain's
    agent CLI is missing, fall back to printing what to run (so automation / CI stays safe).
    """
    import shutil
    from . import orchestrator
    prof = os.path.join(os.getcwd(), ".nishiki")
    has_profile = os.path.exists(os.path.join(prof, "KOI.yaml"))
    brain = orchestrator.configured_brain()
    cli = {"claude_p": "claude", "codex": "codex"}.get(brain)
    # Launchable if interactive AND (no brain yet → start shows the picker / a brain whose CLI is present
    # / openrouter|command which start handles itself).
    can_launch = sys.stdin.isatty() and (
        not brain or brain in ("openrouter", "command") or (cli and shutil.which(cli)))

    if can_launch:
        if has_profile:
            last = _read_last_run(prof)
            if last and last.get("models"):
                tgt = (f"batch {last['gold_batch']}" if last.get("gold_batch") is not None
                       else last.get("gold_data") or "?")
                print(f"📂 Existing profile here — last measured {len(last['models'])} model(s) on {tgt}.")
            print("→ Launching the orchestrator (it will offer to re-measure the same models)…\n")
            _cmd_start(["--target", os.getcwd()])
        else:
            print("→ Launching the orchestrator (it picks the brain and asks which project to measure)…\n")
            _cmd_start([])
        return

    # Fallback (non-interactive / no agent CLI): show what to run.
    if has_profile:
        target = "?"
        try:
            import yaml
            d = yaml.safe_load(open(os.path.join(prof, "KOI.yaml"), encoding="utf-8")) or {}
            target = d.get("target", "?")
        except Exception:  # noqa: BLE001
            pass
        print(f"📂 An existing Nishiki profile is here (target={target})")
        print(f"   {prof}")
        last = _read_last_run(prof)
        if last and last.get("models"):
            tgt = (f"batch {last['gold_batch']}" if last.get("gold_batch") is not None
                   else last.get("gold_data") or "?")
            print(f"   last measured: {len(last['models'])} model(s) [{', '.join(last['models'][:6])}"
                  f"{' …' if len(last['models']) > 6 else ''}] on {tgt}")
        print("\nTo continue ▶  nishiki start")
        print("   (the AI offers to re-measure the SAME models, or re-evaluate / add a source / rebuild)")
    else:
        print("No Nishiki profile here yet (no .nishiki).")
        print("\nJust run this in the project you want to measure ▶  nishiki start")
        print("   (the AI drives from target → source → measurement → KOI table, asking questions)")
    print("\nAll commands: nishiki --help")


def _ensure_cwd():
    """Make sure the shell's cwd is valid before anything reads it (nishiki resolves the profile from cwd).

    On WSL a directory recreated by Windows leaves the shell with a dangling cwd inode, so os.getcwd()
    raises FileNotFoundError. Try to auto-recover by re-resolving $PWD; if that fails, exit with a clear
    one-line instruction instead of a traceback.
    """
    try:
        os.getcwd()
        return
    except OSError:
        pass
    pwd = os.environ.get("PWD")
    if pwd:
        try:
            os.chdir(pwd)            # re-resolves the path string → fresh inode
            os.getcwd()
            return
        except OSError:
            pass
    sys.stderr.write(
        'nishiki: the shell working directory is no longer valid (it was moved or recreated).\n'
        '         Re-enter it and retry, e.g.:  cd "$PWD"   (or: cd /path/to/your/project)\n')
    sys.exit(2)


def main(argv=None):
    try:
        _ensure_cwd()
        _dispatch(argv)
    except KeyboardInterrupt:
        print("\nAborted (no charges).")


def _dispatch(argv):
    argv = sys.argv[1:] if argv is None else argv
    from . import dotenv
    dotenv.autoload()            # load `.env` from cwd (real env vars still win)
    if not argv:
        _smart_entry()
        return
    if argv[0] in ("-h", "--help", "help"):
        _help()
        return
    sub, rest = argv[0], argv[1:]
    if sub == "start":
        _cmd_start(rest)
    elif sub == "calibrate-env":
        _cmd_calibrate_env(rest)
    elif sub == "suggest-floor":
        _cmd_suggest_floor(rest)
    elif sub == "adapter-path":
        _cmd_adapter_path(rest)
    elif sub == "measure":
        _cmd_measure(rest)
    elif sub == "estimate":
        _cmd_estimate(rest)
    elif sub == "watch":
        _cmd_watch(rest)
    elif sub == "relay":
        _cmd_relay(rest)
    elif sub == "run":
        sys.exit(_cmd_run(rest) or 0)
    elif sub == "live":
        sys.exit(_cmd_live(rest) or 0)
    elif sub == "history":
        sys.exit(_cmd_history(rest) or 0)
    elif sub == "koi-report":
        _cmd_koi_report(rest)
    elif sub == "ask":
        _cmd_ask(rest)
    elif sub == "init":
        _cmd_init(rest)
    elif sub == "models":
        _cmd_models(rest)
    elif sub == "config":
        _cmd_config(rest)
    elif sub == "setup":
        _cmd_setup(rest)
    else:
        print(f"unknown subcommand: {sub}\n")
        _help()


if __name__ == "__main__":
    main()
