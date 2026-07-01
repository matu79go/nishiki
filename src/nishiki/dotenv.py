"""Minimal .env loader — no third-party dependency.

Loads `KEY=VALUE` lines from a `.env` file into `os.environ` so credentials
(OPENROUTER_API_KEY, AWS_*, ...) can live in a gitignored `.env` instead of being
exported by hand. By default an **already-set environment variable wins**
(`override=False`), so real env / CI secrets / a container's `~/.aws` take
precedence over a `.env`.

Supported lines: `KEY=value`, `export KEY=value`, `# comments`, blank lines, and
single- or double-quoted values. Anything without `=` is skipped.
"""
import os

__all__ = ["parse_env", "load_env", "autoload"]


def parse_env(text):
    """Parse .env text into a dict (last value wins). Pure; touches no global state."""
    out = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]          # strip matching surrounding quotes
        out[key] = val
    return out


def load_env(path=".env", override=False):
    """Load one .env file into os.environ. Returns the keys actually set (missing file = [])."""
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            pairs = parse_env(f.read())
    except OSError:
        return []
    set_keys = []
    for k, v in pairs.items():
        if override or k not in os.environ:
            os.environ[k] = v
            set_keys.append(k)
    return set_keys


def autoload(*dirs, override=False):
    """Load `.env` from each given dir (default = cwd). Returns all keys set.

    Used at CLI startup (cwd) and per target/experiment dir so the target project's
    own `.env` is picked up. With override=False the first-loaded / pre-existing value
    wins, so call order is: cwd first, then the target/experiment dir.
    """
    loaded, seen = [], set()
    for d in (dirs or (".",)):
        if d is None:
            continue
        try:
            ap = os.path.abspath(os.path.join(d, ".env"))   # abspath needs the cwd; a deleted cwd raises here
        except OSError:
            continue                                        # missing/inaccessible cwd → just skip this dir
        if ap in seen:
            continue
        seen.add(ap)
        loaded += load_env(ap, override=override)
    return loaded
