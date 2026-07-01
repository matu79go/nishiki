"""CUAD candidate-catalog builder — merge bedrock + openrouter and stratify by price tier (design doc §18.9).

Pick a **broad set of candidates** that fit the KPI — not just "the cheapest" — deterministically (for the
cost/quality frontier). No AI quality judgment is involved (stratify = across price bands, by modality/price only).
Quality is decided by probe=measurement.

Output: write NZ_CATALOG (JSON) and NZ_CANDIDATES → consumed by calibrate.py (CUAD glue).

Run (on the data host, inside venv, with AWS_* and OPENROUTER in env / .env):
  export $(grep -E '^AWS_|^OPENROUTER' path/to/.env | tr -d '\r' | xargs -d '\n')
  python adapters/cuad/build_catalog.py --bands 4 --per-band 3 --max-price 5 \
      --catalog-out /tmp/nz_broad.json --cands-out /tmp/nz_cands.txt
"""
import argparse
import json
import os
import sys

try:
    from nishiki import models_cmd as M
except ImportError:  # pragma: no cover
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))
    from nishiki import models_cmd as M


def build(region, bands, per_band, max_price, min_price, include_bedrock=True, per_source=True):
    """Stratify the merged catalog and return (keys, by, info). bedrock is on_demand only (profile
    errors on the base-ID converse, so it is excluded from probe = clean connectivity).

    per_source=True (default): **stratify per source** then merge = both bedrock and openrouter always appear.
      Prevents openrouter (which dominates by count, 309 vs 12) from monopolizing every band, so the
      cross-source cost comparison holds.
    per_source=False: stratify all sources together (price frontier only; source skew is tolerated).
    """
    sources = {}
    if include_bedrock:
        try:
            bed = M.fetch_bedrock_catalog(region=region, vision_only=False)
            mb = M.curate([], bed, origin="bedrock", vision_only=False)
            sources["bedrock"] = [m for m in mb
                                  if m.get("call") == "on_demand" and m.get("in_mtok") is not None]
        except Exception as e:  # noqa: BLE001
            print(f"[warn] bedrock fetch failed ({str(e)[:120]}) → openrouter only", file=sys.stderr)
    orc = [m for m in M.fetch_catalog() if (m.get("in_per_1k") or 0) > 0]
    sources["openrouter"] = M.curate([], orc, origin="openrouter", vision_only=False)
    print("[build] pool: " + " + ".join(f"{o}={len(ms)}" for o, ms in sources.items()),
          file=sys.stderr)

    by = {}
    for ms in sources.values():
        by.update({m["key"]: m for m in ms})
    if per_source:                              # stratify per source → always include both sources
        keys = []
        for origin, ms in sources.items():
            ks, _ = M.select_stratified(ms, bands=bands, per_band=per_band,
                                        max_price=max_price, min_price=min_price)
            keys += ks
            print(f"  [{origin}] {len(ks)} candidates", file=sys.stderr)
        keys = sorted(dict.fromkeys(keys), key=lambda k: by[k]["in_mtok"] or 0)
        priced = [by[k]["in_mtok"] for k in keys if by[k]["in_mtok"] is not None]
        info = {"n_chosen": len(keys), "price_min": min(priced) if priced else None,
                "price_max": max(priced) if priced else None}
    else:
        combined = [m for ms in sources.values() for m in ms]
        keys, info = M.select_stratified(combined, bands=bands, per_band=per_band,
                                         max_price=max_price, min_price=min_price)
    return keys, by, info


def main():
    p = argparse.ArgumentParser(prog="build_catalog")
    p.add_argument("--region", default=os.environ.get("AWS_REGION", "ap-northeast-1"))
    p.add_argument("--bands", type=int, default=4)
    p.add_argument("--per-band", type=int, default=3)
    p.add_argument("--max-price", type=float, default=5.0, help="upper $/Mtok bound (excludes unrealistic values like o1-pro)")
    p.add_argument("--min-price", type=float, default=None)
    p.add_argument("--no-bedrock", action="store_true")
    p.add_argument("--mix-sources", action="store_true",
                   help="stratify all sources together (default=per-source=always emit both bedrock and openrouter)")
    p.add_argument("--catalog-out", default="/tmp/nz_broad.json")
    p.add_argument("--cands-out", default="/tmp/nz_cands.txt")
    args = p.parse_args()

    keys, by, info = build(args.region, args.bands, args.per_band, args.max_price,
                           args.min_price, include_bedrock=not args.no_bedrock,
                           per_source=not args.mix_sources)
    print(f"=== stratified selection {len(keys)} candidates (bands={args.bands} per_band={args.per_band} "
          f"max_price={args.max_price})===", file=sys.stderr)
    for k in keys:
        m = by[k]
        print(f"  [{m['origin']:10}] {k:36} {m['in_mtok']:.3f}/Mtok  call={m['call']}",
              file=sys.stderr)
    print(f"origins: {sorted({by[k]['origin'] for k in keys})}  "
          f"price {info['price_min']}..{info['price_max']}", file=sys.stderr)

    inj = {k: {"model_id": by[k]["model_id"], "in": by[k]["in_mtok"], "out": by[k]["out_mtok"],
               "call": by[k]["call"], "note": by[k].get("note", "")} for k in keys}
    with open(args.catalog_out, "w", encoding="utf-8") as f:
        json.dump(inj, f, ensure_ascii=False)
    with open(args.cands_out, "w", encoding="utf-8") as f:
        f.write(",".join(keys))
    print(f"NZ_CATALOG → {args.catalog_out}\nNZ_CANDIDATES → {args.cands_out}", file=sys.stderr)


if __name__ == "__main__":
    main()
