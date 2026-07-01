"""Unit test for the minimal .env loader (nishiki.dotenv).

Verifies parsing (export/comments/quotes), that real env vars win over .env
(override=False), explicit override=True, and that autoload picks up a dir's .env.

Run: cd nishiki && python3 -m tests.test_dotenv
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)               # project dir (nishiki)
sys.path.insert(0, os.path.join(ROOT, "src"))   # src-layout: package is src/nishiki

from nishiki import dotenv  # noqa: E402


def _write(text):
    d = tempfile.mkdtemp(prefix="nz_env_")
    p = os.path.join(d, ".env")
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return d, p


def test_parse_env():
    """parse_env: export prefix, comments, blanks, quotes, lines without '=' skipped."""
    parsed = dotenv.parse_env(
        "# a comment\n"
        "\n"
        "OPENROUTER_API_KEY=sk-or-123\n"
        "export AWS_REGION=ap-northeast-1\n"
        'QUOTED="has spaces"\n'
        "SINGLE='v'\n"
        "NOT_A_PAIR\n"
    )
    assert parsed["OPENROUTER_API_KEY"] == "sk-or-123"
    assert parsed["AWS_REGION"] == "ap-northeast-1"       # export prefix stripped
    assert parsed["QUOTED"] == "has spaces"               # surrounding quotes stripped
    assert parsed["SINGLE"] == "v"
    assert "NOT_A_PAIR" not in parsed                     # no '=' → skipped
    assert "# a comment" not in parsed
    print("  ✓ parse_env: export/comment/blank/quotes/no-'=' handled")


def test_load_env_no_override():
    """load_env: sets a missing var, but an already-set env var wins (override=False)."""
    key_new, key_set = "NZ_TEST_NEW", "NZ_TEST_EXISTING"
    os.environ.pop(key_new, None)
    os.environ[key_set] = "from_real_env"
    try:
        _d, p = _write(f"{key_new}=from_dotenv\n{key_set}=from_dotenv\n")
        keys = dotenv.load_env(p)
        assert os.environ[key_new] == "from_dotenv"        # filled because it was missing
        assert os.environ[key_set] == "from_real_env"      # real env wins
        assert key_new in keys and key_set not in keys     # only the newly-set key is reported
        print("  ✓ load_env: fills missing, real env wins (override=False)")
    finally:
        os.environ.pop(key_new, None)
        os.environ.pop(key_set, None)


def test_load_env_override_and_missing_file():
    """load_env: override=True replaces; a missing file is a no-op ([])."""
    key = "NZ_TEST_OVERRIDE"
    os.environ[key] = "old"
    try:
        _d, p = _write(f"{key}=new\n")
        dotenv.load_env(p, override=True)
        assert os.environ[key] == "new"
        assert dotenv.load_env(os.path.join(_d, "does_not_exist.env")) == []
        print("  ✓ load_env: override=True replaces / missing file = no-op")
    finally:
        os.environ.pop(key, None)


def test_autoload_dir():
    """autoload(dir): loads <dir>/.env; dedups repeated dirs."""
    key = "NZ_TEST_AUTOLOAD"
    os.environ.pop(key, None)
    try:
        d, _p = _write(f"{key}=loaded\n")
        keys = dotenv.autoload(d, d)                       # repeated dir must not double-load
        assert os.environ.get(key) == "loaded"
        assert keys.count(key) == 1
        print("  ✓ autoload: loads dir/.env, dedups repeated dirs")
    finally:
        os.environ.pop(key, None)


def main():
    fails = 0
    for fn in (test_parse_env, test_load_env_no_override,
               test_load_env_override_and_missing_file, test_autoload_dir):
        try:
            print(f"[{fn.__name__}]")
            fn()
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"  ✗ FAIL: {type(e).__name__}: {e}")
    print("\n" + ("✅ all tests PASS" if not fails else f"❌ {fails} FAIL"))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
