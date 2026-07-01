"""Orchestrator "brain" (the LLM that authors the config) = chosen by the user first (design doc §18.2).

Idea: most developers already have some LLM tool/subscription. **Pick one and use it**:
  - claude_p   : local `claude -p` (Claude Code subscribers. I, the developer, use this too)
  - codex      : OpenAI Codex CLI (`codex exec`)
  - copilot    : GitHub Copilot CLI
  - openrouter : OpenRouter /chat/completions (one key for proprietary+open. The default for those without a subscription)
  - command    : any CLI specified via your own template (`{prompt}` is substituted)

Chosen once. Saved to `~/.nishiki/config.json` via `nishiki config --brain <name>`.
Strict users can assign an in-region brain (Bedrock jp. etc.) via command to satisfy the hosting bar too.
"""
import json
import os
import shutil
import subprocess
import urllib.request

CONFIG_PATH = os.path.expanduser("~/.nishiki/config.json")
OPENROUTER_CHAT = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_OR_MODEL = "anthropic/claude-opus-4.6-fast"

# CLI brain presets (argv template. {prompt} is substituted as a single arg = no shell = safe).
CLI_PRESETS = {
    "claude_p": ["claude", "-p", "{prompt}"],
    "codex":    ["codex", "exec", "{prompt}"],
    "copilot":  ["copilot", "-p", "{prompt}"],
}
BRAINS = list(CLI_PRESETS) + ["openrouter", "command"]


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return CONFIG_PATH


def available_brains():
    """Whether each brain is available in this environment right now (CLI detect / key detect). For the selection menu."""
    rows = []
    for name, argv in CLI_PRESETS.items():
        rows.append((name, shutil.which(argv[0]) is not None, argv[0]))
    rows.append(("openrouter", bool(os.getenv("OPENROUTER_API_KEY")), "OPENROUTER_API_KEY"))
    return rows


def configured_brain():
    return load_config().get("brain")


def _call_cli(argv_tmpl, prompt, timeout):
    argv = [a.replace("{prompt}", prompt) for a in argv_tmpl]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"{argv[0]} failed: {r.stderr[:400]}")
    return r.stdout


def _call_openrouter(prompt, model, timeout):
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set (required for the openrouter brain)")
    body = {"model": model, "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(
        OPENROUTER_CHAT, data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)["choices"][0]["message"]["content"]


def call(prompt, backend=None, model=None, timeout=240):
    """Call the chosen brain once. If none is chosen, raise an error prompting selection."""
    cfg = load_config()
    brain = backend or cfg.get("brain")
    if not brain:
        names = ", ".join(n for n, _, _ in available_brains())
        raise RuntimeError(
            "No brain selected. Choose one first: `nishiki config --brain <name>`\n"
            f"  candidates: {names}")
    if brain == "openrouter":
        return _call_openrouter(prompt, model or cfg.get("model") or DEFAULT_OR_MODEL, timeout)
    if brain in CLI_PRESETS:
        return _call_cli(CLI_PRESETS[brain], prompt, timeout)
    if brain == "command":
        argv = cfg.get("argv")
        if not argv:
            raise RuntimeError("brain=command requires argv in config (e.g. [\"codex\",\"exec\",\"{prompt}\"])")
        return _call_cli(argv, prompt, timeout)
    raise ValueError(f"unknown brain: {brain}")
