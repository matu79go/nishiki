"""Unit + loopback test for the B2 relay (nishiki.relay) — no external network, no model calls.

extract_event maps the request body → a live event (text + inline base64 image tokens). The loopback
test runs the real relay against a local fake upstream: a POST is forwarded verbatim and one event is
appended to live.jsonl.
"""
import base64
import json
import os
import struct
import sys
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from nishiki import relay as R  # noqa: E402


def _png_data_url(w, h):
    raw = (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
           + struct.pack(">II", w, h) + b"\x08\x06\x00\x00\x00" + b"\x00" * 8)
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


def _profile():
    d = tempfile.mkdtemp(prefix="nz_relay_")
    with open(os.path.join(d, "MODELS.yaml"), "w", encoding="utf-8") as f:
        f.write("models:\n  m32: { model_id: vendor/m-32b, in: 0.1, out: 0.4 }\n")
    return d


def test_extract_event():
    body = {"model": "vendor/m-32b", "max_tokens": 99, "messages": [
        {"role": "user", "content": [
            {"type": "text", "text": "verify this"},
            {"type": "image_url", "image_url": {"url": _png_data_url(800, 600)}}]}]}
    ev = R.extract_event(body, {"vendor/m-32b": "m32"})
    assert ev["model"] == "m32"                                   # model_id → profile key
    assert ev["out_tokens"] == 99
    from nishiki import koi_estimate as KE
    expect_in = KE.text_tokens("verify this") + KE.image_tokens(800, 600)
    assert ev["in_tokens"] == expect_in                          # text + inline image tokens
    # plain string content + no model_map → model_id passes through, max_tokens default
    ev2 = R.extract_event({"model": "x", "messages": [{"role": "user", "content": "hi"}]})
    assert ev2["model"] == "x" and ev2["out_tokens"] == KE._DEFAULT_OUT_TOKENS
    print("  ✓ extract_event: model mapped / text+image tokens / max_tokens / string content")


def test_relay_forwards_and_logs():
    canned = b'{"choices":[{"message":{"content":"OK"}}]}'

    class Fake(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(canned)))
            self.end_headers()
            self.wfile.write(canned)

    up = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    exp = _profile()
    srv = R.make_server(exp, 0, upstream=f"http://127.0.0.1:{up.server_address[1]}")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        body = json.dumps({"model": "vendor/m-32b", "max_tokens": 50,
                           "messages": [{"role": "user", "content": "hello"}]}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/chat/completions",
                                     data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            got = r.read()
        assert got == canned, "relay must forward the upstream response verbatim"
        with open(os.path.join(exp, "live.jsonl"), encoding="utf-8") as f:
            ev = json.loads(f.read().splitlines()[-1])
        assert ev["model"] == "m32" and ev["out_tokens"] == 50 and ev["in_tokens"] >= 1
        print("  ✓ relay: forwards response verbatim + appends one event to live.jsonl")
    finally:
        srv.shutdown(); srv.server_close()
        up.shutdown(); up.server_close()


def main():
    fails = 0
    for fn in (test_extract_event, test_relay_forwards_and_logs):
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
