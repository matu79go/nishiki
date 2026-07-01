"""Runtime probe — wrap the target's model-call (the choke) and log usage to live.jsonl.

`nishiki run` injects this via a temp `sitecustomize.py` (PYTHONPATH) so it loads at interpreter
startup in the agent's process, WITHOUT editing the agent's source. It wraps the choke function named
in env `NZ_PROBE_CHOKE` ("module:func"); after each call it maps the call to a live event and appends
it to `NZ_PROBE_LOG`, then returns the original result untouched. Provider-agnostic: it intercepts at
the function boundary, so it does not care whether the function uses Bedrock / OpenRouter / a raw API.

The call→event mapping is a small adapter (provider/target-specific only in how it reads model+tokens):
- default_adapter: best-effort from kwargs (model/prompt/images/max_tokens) — estimates tokens locally.
- a custom adapter ("module:func" in NZ_PROBE_ADAPTER) can return exact usage from the result.
Both run inside a try/except and never affect the wrapped call.
"""
import importlib
import importlib.abc
import importlib.util
import json
import os
import sys

_LOG_ENV, _CHOKE_ENV, _ADAPTER_ENV = "NZ_PROBE_LOG", "NZ_PROBE_CHOKE", "NZ_PROBE_ADAPTER"


def append_event(log, event):
    """Append one event as a JSON line; best-effort, never raises."""
    try:
        os.makedirs(os.path.dirname(log) or ".", exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _usage_from_result(result):
    """Exact {model, in_tokens, out_tokens} from a return value that carries usage (provider-agnostic).

    Handles the common shapes — a dict with input_tokens/output_tokens (+ model_id), a nested `usage`
    (inputTokens/outputTokens or prompt_tokens/completion_tokens), or an OpenAI-style object with
    `.usage` + `.model`. Returns None if no usage is present.
    """
    def pick(d, *names):
        for n in names:
            v = d.get(n) if isinstance(d, dict) else getattr(d, n, None)
            if v is not None:
                return v
        return None

    if isinstance(result, dict):
        d, u = result, (result.get("usage") if isinstance(result.get("usage"), dict) else {})
    else:                                                        # object (e.g. OpenAI response)
        d = result
        u = getattr(result, "usage", None) or {}
        u = u if isinstance(u, dict) else u.__dict__ if hasattr(u, "__dict__") else {}
        if not (hasattr(result, "model") or hasattr(result, "usage")):
            return None
        d = {"model": getattr(result, "model", None)}
    model = pick(d, "model_id", "model")
    inp = pick(d, "input_tokens", "inputTokens", "prompt_tokens") or pick(u, "input_tokens", "inputTokens", "prompt_tokens")
    out = pick(d, "output_tokens", "outputTokens", "completion_tokens") or pick(u, "output_tokens", "outputTokens", "completion_tokens")
    if model is not None and (inp is not None or out is not None):
        return {"model": model, "in_tokens": int(inp or 0), "out_tokens": int(out or 0)}
    return None


def default_adapter(args, kwargs, result):
    """{model, in_tokens, out_tokens} for a call — EXACT usage from the result if present, else estimated.

    Most chokes return usage (e.g. an OpenAI/Bedrock response) → exact, zero config. Else
    fall back to estimating tokens from the prompt/images found in kwargs or the first positional args.
    """
    from . import koi_estimate
    exact = _usage_from_result(result)
    if exact:
        return exact
    # fallback: estimate from kwargs, or positional args (model_id, prompt, ...) for positional chokes
    model = kwargs.get("model") or kwargs.get("model_id") or kwargs.get("model_key")
    prompt = kwargs.get("prompt") or kwargs.get("text")
    if model is None and args and isinstance(args[0], str):
        model = args[0]
    if prompt is None and len(args) > 1 and isinstance(args[1], str):
        prompt = args[1]
    if model is None:
        return None
    in_tok = koi_estimate.text_tokens(prompt) if isinstance(prompt, str) else 0
    for im in (kwargs.get("images") or []):
        data = im[1] if isinstance(im, (tuple, list)) and len(im) > 1 else im
        dims = (koi_estimate.image_dims_bytes(data) if isinstance(data, (bytes, bytearray))
                else koi_estimate.image_dims(data) if isinstance(data, str) and os.path.exists(data)
                else None)
        in_tok += koi_estimate.image_tokens(*dims) if dims else 1500
    out_tok = kwargs.get("max_tokens") or koi_estimate._DEFAULT_OUT_TOKENS
    return {"model": model, "in_tokens": in_tok, "out_tokens": out_tok}


def _load_adapter(spec):
    if not spec:
        return default_adapter
    mod, _, fn = spec.partition(":")
    try:
        return getattr(importlib.import_module(mod), fn)
    except Exception:  # noqa: BLE001
        return default_adapter


def _wrap(orig, adapter, log):
    import time
    def wrapper(*args, **kwargs):
        t0 = time.perf_counter()
        result = orig(*args, **kwargs)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0          # real wall time of the actual call
        try:
            ev = adapter(args, kwargs, result)
            if ev:
                ev.setdefault("ts", time.time())                 # stamp time for the history view
                ev.setdefault("latency_ms", round(elapsed_ms))   # actual-route latency (separate axis)
                append_event(log, ev)
        except Exception:  # noqa: BLE001 — observation must never break the real call
            pass
        return result
    wrapper.__wrapped__ = orig
    return wrapper


def patch_module(module, func_name, adapter, log):
    """Wrap module.func_name in place (idempotent). Returns True if wrapped."""
    fn = getattr(module, func_name, None)
    if fn is None or getattr(fn, "__wrapped__", None) is not None:
        return False
    setattr(module, func_name, _wrap(fn, adapter, log))
    return True


class _ChokeFinder(importlib.abc.MetaPathFinder):
    """Patch the choke function right after its module finishes importing (lazy, robust)."""

    def __init__(self, target, func, adapter, log):
        self.target, self.func, self.adapter, self.log, self._busy = target, func, adapter, log, False

    def find_spec(self, name, path, target=None):
        if name != self.target or self._busy:
            return None
        self._busy = True                                        # avoid recursing into ourselves
        try:
            spec = importlib.util.find_spec(name)
        finally:
            self._busy = False
        if spec is None or spec.loader is None:
            return None
        orig_exec = spec.loader.exec_module

        def exec_module(module, _orig=orig_exec):
            _orig(module)
            patch_module(module, self.func, self.adapter, self.log)
        spec.loader.exec_module = exec_module
        return spec


def install():
    """Read env and arrange to wrap the choke. Called from the injected sitecustomize."""
    choke, log = os.environ.get(_CHOKE_ENV), os.environ.get(_LOG_ENV)
    if not choke or not log or ":" not in choke:
        return
    mod_name, func_name = choke.split(":", 1)
    adapter = _load_adapter(os.environ.get(_ADAPTER_ENV))
    if mod_name in sys.modules:                                  # already imported → patch now
        patch_module(sys.modules[mod_name], func_name, adapter, log)
    else:                                                        # else patch on import
        sys.meta_path.insert(0, _ChokeFinder(mod_name, func_name, adapter, log))
