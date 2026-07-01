"""Live KOI HUD — tail `.nishiki/live.jsonl` and render an in-place terminal view (no model calls).

Each line of `live.jsonl` is one captured model call (an "event"), e.g.
  {"model": "qwen3-vl-32b", "image": "/path/page.png", "prompt": "Verify…", "out_tokens": 256}
For every new event, the estimator (koi_estimate) computes KOI from the prompt/input locally and the
HUD redraws the same screen region. v1 events are appended manually or by the B2 relay (next slice).

Pure helpers (parse_event / rolling_stats / render_frame) are unit-tested; the redraw loop lives in
the CLI (`nishiki watch`).
"""
import json

from . import koi_estimate

EVENT_FIELDS = ("model", "prompt", "prompt_file", "image", "in_tokens", "out_tokens")


def parse_event(line):
    """Parse one `live.jsonl` line into an event dict, or None if blank/garbage."""
    line = (line or "").strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    ev = {k: obj[k] for k in EVENT_FIELDS if k in obj}
    if ev.get("prompt_file") and "prompt" not in ev:
        try:
            with open(ev["prompt_file"], encoding="utf-8") as f:
                ev["prompt"] = f.read()
        except OSError:
            pass
    return ev


def estimate_event(experiment, event, *, basis=None):
    """Run the edge estimate for one event (all models; the event's `model` = current route)."""
    return koi_estimate.estimate(
        experiment, prompt=event.get("prompt"), image=event.get("image"),
        in_tokens=event.get("in_tokens"), out_tokens=event.get("out_tokens"), basis=basis)


def rolling_stats(history):
    """Aggregate the current-route cost/KOI across recent events → {n, avg_cost, avg_koi, drift_pct}.

    `history` = list of (cost_per_item, koi) for the route actually used in each recent event.
    drift_pct = half the peak-to-peak KOI spread as a % of the mean (a cheap volatility readout).
    """
    kois = [k for _, k in history if k is not None]
    costs = [c for c, _ in history if c is not None]
    if not kois:
        return {"n": len(history), "avg_cost": None, "avg_koi": None, "drift_pct": None}
    avg_koi = sum(kois) / len(kois)
    drift = (max(kois) - min(kois)) / 2 / avg_koi * 100 if avg_koi else None
    return {"n": len(history),
            "avg_cost": (sum(costs) / len(costs)) if costs else None,
            "avg_koi": avg_koi, "drift_pct": drift}


def _money(x):
    return "-" if x is None else f"${x:.5f}"


def _koi(x):
    return "-" if x is None else (f"{x/1000:.0f}k" if x >= 10000 else f"{x:.0f}")


def render_frame(result, route, stats, *, now_str, width=60):
    """Build the framed live-KOI view as a string (no ANSI; the loop handles cursor/clear)."""
    rows = sorted(result["rows"], key=lambda r: (r["koi"] is None, -(r["koi"] or 0)))
    ref = result.get("reference")
    cur = next((r for r in rows if r["model"] == route), None)

    def bar(label):
        return "│" + label + " " * max(1, width - 2 - len(label)) + "│"

    title, note = " Nishiki live KOI ", " (no model calls) "
    out = ["┌" + title + "─" * max(1, width - 2 - len(title) - len(note)) + note + "┐"]

    # input summary
    if cur and cur.get("in_tokens") is not None:
        inp = f" input: ~{cur['in_tokens']} in / {cur['out_tokens']} out"
    else:
        inp = " input: (measured cost reused)"
    if result.get("image_dims"):
        inp = f" image {result['image_dims'][0]}x{result['image_dims'][1]}," + inp
    out.append(bar(inp))
    out.append(bar(f" current route: {route or '?'}"))
    head = f"   {'model':18}{'$/item':>10}{'KOI':>8}" + (f"   {'vs ' + ref:>8}" if ref else "")
    out.append(bar(head))

    for r in rows:
        mark = "▸" if r["model"] == route else " "
        star = "  ⭐" if (r is rows[0] and r["koi"] is not None) else ""
        name = r["model"] + (" (ref)" if r["model"] == ref else "")
        vs = ""
        if ref:
            v = r.get("vs_reference")
            vs = f"   {('-' if v is None else f'{v:.1f}x'):>8}"
        out.append(bar(f" {mark} {name:18}{_money(r['cost_per_item']):>10}{_koi(r['koi']):>8}{vs}{star}"))

    if stats and stats.get("avg_koi") is not None:
        d = "" if stats["drift_pct"] is None else f" · drift ±{stats['drift_pct']:.0f}%"
        out.append(bar(f" rolling(last {stats['n']}): avg {_money(stats['avg_cost'])} · "
                       f"KOI {_koi(stats['avg_koi'])}{d}"))

    tail = f" updated {now_str} "
    out.append("└" + "─" * max(1, width - 2 - len(tail)) + tail + "┘")
    return "\n".join(out)
