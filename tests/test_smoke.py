"""Smoke test = regression net for the structural refactor (2026-06-21, locked before going OSS).

Quickly detect "not broken" before/after touching files. Checks:
  - Each module of the production KOI flow imports (detects split / broken imports).
  - CLI starts and help + the main subcommands' argparse pass (dispatch is healthy).

Run: cd nishiki && python3 -m tests.test_smoke
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)            # project dir (nishiki)
SRC = os.path.join(ROOT, "src")         # src-layout: package is src/nishiki
sys.path.insert(0, SRC)

# Production KOI flow modules (these must always be importable = the self-contained core)
# Note: calibrate (glue) depends on `import constants` inside example_target = a container-side
#       script, so it is not standalone-importable (= rationale for moving it to adapters/). Not listed here.
PROD_MODULES = [
    "nishiki.__main__", "nishiki.init_cmd", "nishiki.models_cmd",
    "nishiki.suggest_floor", "nishiki.koi_report", "nishiki.orchestrator",
]

# Subcommands with argparse (--help returns 0 = that _cmd and its deps are importable)
HELP_CMDS = ["init", "calibrate-env", "suggest-floor", "koi-report", "models"]


def _run(args):
    # src-layout: pass PYTHONPATH=src so it works without pip install (prod is pip install -e .)
    env = dict(os.environ, PYTHONPATH=SRC + os.pathsep + os.environ.get("PYTHONPATH", ""))
    return subprocess.run([sys.executable, "-m", "nishiki", *args],
                          cwd=ROOT, capture_output=True, text=True, timeout=60, env=env)


def test_prod_modules_import():
    """Production flow modules import (detects broken imports / splits)."""
    import importlib
    for name in PROD_MODULES:
        importlib.import_module(name)
    print(f"  ✓ all {len(PROD_MODULES)} production modules import OK")


def test_cli_help():
    """`nishiki --help` lists all commands, and bare `nishiki` is the smart entry (guides to start)."""
    r = _run(["--help"])
    assert r.returncode == 0, f"--help exited abnormally: {r.stderr[:200]}"
    out = r.stdout + r.stderr
    for kw in ("nishiki", "start", "init", "calibrate"):
        assert kw in out, f"--help missing {kw}"
    # no args = smart entry (detects .nishiki and guides to start)
    r0 = _run([])
    assert r0.returncode == 0 and "start" in (r0.stdout + r0.stderr), "bare nishiki does not guide to start"
    print("  ✓ --help all commands + bare nishiki = smart entry (guides to start)")


def test_subcommand_help_parses():
    """Main subcommands' `--help` return 0 (= their _cmd and deps import)."""
    for cmd in HELP_CMDS:
        r = _run([cmd, "--help"])
        assert r.returncode == 0, f"`{cmd} --help` exited abnormally rc={r.returncode}: {r.stderr[:200]}"
    print(f"  ✓ all {len(HELP_CMDS)} subcommand --help pass argparse")


def test_last_run_roundtrip():
    """re-measure state: _write_last_run drops None fields; _read_last_run round-trips; missing = None."""
    import tempfile
    from nishiki import __main__ as m
    d = tempfile.mkdtemp(prefix="nz_lastrun_")
    assert m._read_last_run(d) is None, "no last_run.json yet → None"
    m._write_last_run(d, models=["qwen-vl", "c2"], mode="run", gold_batch=6472,
                      gold_data=None, winner="qwen-vl", via="measure")
    got = m._read_last_run(d)
    assert got["models"] == ["qwen-vl", "c2"] and got["gold_batch"] == 6472
    assert got["winner"] == "qwen-vl" and got["via"] == "measure"
    assert "gold_data" not in got, "None fields are dropped"
    print("  ✓ last_run: write drops None / read round-trips / missing = None")


def test_bare_nishiki_launches_orchestrator():
    """Bare `nishiki` in a TTY = launch the orchestrator: no profile → start([]); profile → start(--target cwd)."""
    import tempfile
    from nishiki import __main__ as m
    from nishiki import orchestrator
    calls, cwd0 = [], os.getcwd()
    orig_start, orig_brain, orig_stdin = m._cmd_start, orchestrator.configured_brain, sys.stdin

    class _TTY:
        def isatty(self):
            return True
    try:
        m._cmd_start = lambda argv: calls.append(list(argv))
        orchestrator.configured_brain = lambda: "command"   # launchable without a real binary
        sys.stdin = _TTY()
        d1 = tempfile.mkdtemp(prefix="nz_bare1_"); os.chdir(d1)
        m._smart_entry()
        d2 = tempfile.mkdtemp(prefix="nz_bare2_"); os.makedirs(os.path.join(d2, ".nishiki"))
        with open(os.path.join(d2, ".nishiki", "KOI.yaml"), "w", encoding="utf-8") as f:
            f.write("target: x\n")
        os.chdir(d2)
        m._smart_entry()
        assert calls[0] == [], calls[0]                       # first time → start, AI asks the path
        assert calls[1] == ["--target", d2], calls[1]         # existing profile → start pointed here
    finally:
        m._cmd_start, orchestrator.configured_brain, sys.stdin = orig_start, orig_brain, orig_stdin
        os.chdir(cwd0)
    print("  ✓ bare nishiki: TTY launches orchestrator (start[] / start --target cwd)")


def main():
    fails = 0
    for fn in (test_prod_modules_import, test_cli_help, test_subcommand_help_parses,
               test_last_run_roundtrip, test_bare_nishiki_launches_orchestrator):
        try:
            print(f"[{fn.__name__}]")
            fn()
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"  ✗ FAIL: {type(e).__name__}: {e}")
    print("\n" + ("✅ all smoke PASS" if not fails else f"❌ {fails} FAIL"))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
