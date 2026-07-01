"""LLM call backends — OpenAI-compatible (OpenRouter) / Bedrock converse.

Shared by the generic adapter (generic_adapter) and target-specific glue. Every backend returns the same shape:
  {"text": str, "input_tokens": int, "output_tokens": int}

The candidate call kind → backend mapping is `backend_for`:
  openai_compatible → OpenRouter (key OPENROUTER_API_KEY) / on_demand|profile → Bedrock (boto3, AWS_*).
"""
import base64
import json
import os
import urllib.error
import urllib.request

_OPENAI_BASE = os.environ.get("NZ_OPENAI_BASE", "https://openrouter.ai/api/v1").rstrip("/")
_OPENAI_URL = _OPENAI_BASE + "/chat/completions"


def openai_chat(model_id, prompt, max_tokens=1024, images=None):
    """Call OpenAI-compatible chat/completions once. If images=[(fmt,bytes)] is given, send as vision."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set (required for the openai_compatible backend)")
    content = []
    for fmt, data in (images or []):
        mime = "image/jpeg" if fmt in ("jpeg", "jpg") else f"image/{fmt}"
        b64 = base64.b64encode(data).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
    content.append({"type": "text", "text": prompt})
    body = json.dumps({"model": model_id, "max_tokens": max_tokens,
                       "messages": [{"role": "user", "content": content}]}).encode("utf-8")
    req = urllib.request.Request(
        _OPENAI_URL, data=body, method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                 "HTTP-Referer": "https://github.com/matu79go/nishiki",
                 "X-Title": "nishiki"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"OpenRouter {e.code}: {detail}") from e
    msg = (resp.get("choices") or [{}])[0].get("message", {}) or {}
    text = msg.get("content") or ""
    if isinstance(text, list):
        text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
    usage = resp.get("usage", {}) or {}
    return {"text": text, "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0)}


_BEDROCK_CLIENT = None


def bedrock_chat(model_id, prompt, max_tokens=1024):
    """Call Bedrock converse once (text only). AWS auth comes from env (AWS_ACCESS_KEY_ID etc.)."""
    global _BEDROCK_CLIENT
    if _BEDROCK_CLIENT is None:
        import boto3  # lazy import (boto3 not needed if using openrouter only)
        _BEDROCK_CLIENT = boto3.client(
            "bedrock-runtime", region_name=os.environ.get("AWS_REGION", "ap-northeast-1"))
    r = _BEDROCK_CLIENT.converse(
        modelId=model_id, messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": max_tokens})
    blocks = r.get("output", {}).get("message", {}).get("content", []) or []
    text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
    u = r.get("usage", {}) or {}
    return {"text": text, "input_tokens": u.get("inputTokens", 0),
            "output_tokens": u.get("outputTokens", 0)}


def backend_for(call_kind):
    """Candidate call kind → backend function. openai_compatible=OpenRouter / on_demand|profile=Bedrock."""
    return bedrock_chat if call_kind in ("on_demand", "profile") else openai_chat
