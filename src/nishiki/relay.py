"""B2 relay — a transparent local proxy for an OpenAI-compatible (OpenRouter) endpoint.

Point your agent's base URL at this relay (e.g. NZ_OPENAI_BASE / OPENROUTER_BASE_URL =
http://localhost:8900). For each `/chat/completions` request it: (1) reads the prompt/images from
the body and appends one event to `<experiment>/live.jsonl` (so `nishiki watch` updates live), then
(2) forwards the request **unchanged** to the real upstream and returns its response verbatim.

It adds **no** model calls — it only observes the call the agent was already making. Logging is
best-effort and never blocks or alters the forwarded request. Bedrock (AWS-signed) is not handled here.
"""
import base64
import json
import os
import urllib.error
import urllib.request

from . import koi_estimate

_FALLBACK_IMAGE_TOKENS = 1500   # when an inline image's dimensions can't be parsed


def build_model_map(experiment):
    """{upstream model_id → profile candidate key} from MODELS.yaml, so events use the profile's keys."""
    import yaml
    p = os.path.join(experiment, "MODELS.yaml")
    if not os.path.exists(p):
        return {}
    models = (yaml.safe_load(open(p, encoding="utf-8")) or {}).get("models", {})
    return {m.get("model_id"): k for k, m in models.items() if m.get("model_id")}


def _image_tokens_from_data_url(url):
    if not isinstance(url, str) or not url.startswith("data:"):
        return _FALLBACK_IMAGE_TOKENS
    try:
        data = base64.b64decode(url.split(",", 1)[1])
    except (ValueError, IndexError):
        return _FALLBACK_IMAGE_TOKENS
    dims = koi_estimate.image_dims_bytes(data)
    return koi_estimate.image_tokens(*dims) if dims else _FALLBACK_IMAGE_TOKENS


def extract_event(body, model_map=None):
    """Turn an OpenAI chat/completions request body into a live event (pure; no I/O, no model call).

    Returns {model, in_tokens, out_tokens} — model resolved to the profile key when known.
    """
    model_id = body.get("model")
    model = (model_map or {}).get(model_id, model_id)
    texts, img_tokens = [], 0
    for msg in body.get("messages") or []:
        content = msg.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    texts.append(part.get("text", ""))
                elif part.get("type") == "image_url":
                    img_tokens += _image_tokens_from_data_url((part.get("image_url") or {}).get("url"))
    in_tokens = koi_estimate.text_tokens(" ".join(texts)) + img_tokens
    out_tokens = body.get("max_tokens") or koi_estimate._DEFAULT_OUT_TOKENS
    return {"model": model, "in_tokens": in_tokens, "out_tokens": out_tokens}


def append_event(log_path, event):
    """Append one event as a JSON line. Best-effort — never raises."""
    try:
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass


def make_server(experiment, port, *, upstream="https://openrouter.ai/api/v1", log=None, on_event=None):
    """Build (don't start) a threaded HTTP relay. Returns the server; call .serve_forever()."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    upstream = upstream.rstrip("/")
    log_path = log or os.path.join(experiment, "live.jsonl")
    model_map = build_model_map(experiment)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):                              # silence default access logging
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            # observe (best-effort; never blocks the forward)
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
                if isinstance(body, dict) and body.get("messages") is not None:
                    import time
                    ev = extract_event(body, model_map)
                    ev.setdefault("ts", time.time())             # stamp time for the history view
                    append_event(log_path, ev)
                    if on_event:
                        on_event(ev)
            except (ValueError, UnicodeDecodeError):
                pass
            # forward verbatim to the real upstream
            req = urllib.request.Request(upstream + self.path, data=raw, method="POST")
            for k, v in self.headers.items():
                if k.lower() not in ("host", "content-length", "connection"):
                    req.add_header(k, v)
            try:
                with urllib.request.urlopen(req, timeout=600) as r:
                    payload, status = r.read(), r.status
            except urllib.error.HTTPError as e:
                payload, status = e.read(), e.code
            except urllib.error.URLError as e:
                payload, status = json.dumps({"error": str(e)}).encode(), 502
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.nishiki_log = log_path
    server.nishiki_upstream = upstream
    return server
