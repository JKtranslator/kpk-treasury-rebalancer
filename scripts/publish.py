"""Build the static page data from the latest run of each client.

    python publish.py [--runs <dir>] [--site <dir>] [--clients ens nexus ...]

For each client, takes the newest dated folder under runs/<client>/ and writes
data/<client>.json: a compact snapshot the page renders and re-simulates client-side
(positions with APY, idle, in-flight, permitted venues with APY/TVL, policy block, reconciliation
summary, flags, ops-tools optimizer summary). Also writes data/index.json listing clients and
their snapshot timestamps. No secrets or raw API payloads are copied.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

from common import client, fnum, registry, write_json

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def latest_run(runs: Path, slug: str) -> Path | None:
    d = runs / slug
    if not d.exists():
        return None
    dated = sorted([p for p in d.iterdir() if p.is_dir() and (p / "assessment.json").exists()])
    return dated[-1] if dated else None


def snapshot(slug: str, run: Path) -> dict:
    c = client(slug)
    a = json.loads((run / "assessment.json").read_text(encoding="utf-8"))
    h = json.loads((run / "holdings.json").read_text(encoding="utf-8"))
    y = json.loads((run / "yields.json").read_text(encoding="utf-8"))
    book = [dict(kind=b["kind"], protocol=b["protocol"], venue=b["venue"], symbol=b.get("symbol"),
                 asset_group=b["asset_group"], usd=round(fnum(b["usd"]), 2),
                 apy=b.get("apy"), apy_source=b.get("apy_source"), venue_tvl_usd=b.get("venue_tvl_usd"),
                 untracked=b.get("untracked", False), balance=b.get("balance")) for b in a["book"] if fnum(b["usd"]) >= 1]
    permitted = [dict(protocol=p["protocol"], asset=p["asset"], action=p["action"], asset_group=p["asset_group"],
                      apy=p.get("apy_total"), apy_30d=p.get("apy_30d"), tvl_usd=p.get("tvl_usd"), priced=p["priced"],
                      vault=p.get("vault")) for p in y.get("permitted", []) if p["action"] in ("deposit", "stake", "swap")]
    ob = y.get("ops_tools_best_strategy") or {}
    ops = [dict(asset_group=r["assetGroup"], current_apy=fnum(r["currentWeightedAPY"]),
                recommended_apy=fnum(r["recommendedWeightedAPY"]), changed_usd=fnum(r["changedValue"]),
                allocations=[dict(protocol=x["protocol"], venue=x["vaultName"], usd=fnum(x["recommendedValue"]), apy=fnum((x.get("apy") or {}).get("total")))
                             for x in r["allocations"] if fnum(x["recommendedValue"]) > 0])
           for r in ob.get("recommendations", [])]
    n = h["nav"]
    return dict(
        client=slug, display_name=c["display_name"], chain_id=h["chain_id"], avatar_safe=h["avatar_safe"],
        as_of=h["as_of"], period=h["period"], vault_data_fetched_at=y.get("vault_data_fetched_at"),
        run_folder=run.name,
        nav_usd=round(fnum(a["nav_usd"])), eth_price_usd=n.get("eth_price_usd"),
        reconciliation=dict(syncrone_nav_usd=n["syncrone_nav_usd"], bridge_usd=n["bridge_usd"],
                            diff=n.get("syncrone_vs_bridge_diff"), in_flight_usd=n["syncrone_in_flight_usd"],
                            idle_usd=n["syncrone_idle_usd"], untracked_usd=n["untracked_by_strategy_api_usd"],
                            strategy_api_usd=n["strategy_api_positions_usd"],
                            tokens_checked=len(h["reconciliation"]["tokens"]),
                            tokens_disagree=sum(1 for t in h["reconciliation"]["tokens"]
                                                if t["safe_vs_etherscan"] is not None and t["safe_vs_etherscan"] > 0.01),
                            other_wallets=h.get("other_wallets_in_syncrone_org", {})),
        flags=h["flags"], policy=c.get("policy"), policy_checks=a["policy_checks"],
        thresholds=dict(min_pickup_bps=a["thresholds"]["min_pickup_bps"], min_move_usd=a["thresholds"]["min_move_usd"],
                        venue_tvl_cap_pct=a["thresholds"]["venue_tvl_cap_pct"], exclude=a["thresholds"]["exclude"]),
        book=book, permitted=permitted, ops_tools=ops, fallback_apy=y.get("fallback_apy") or {},
        safes=c["safes"].get(str(h["chain_id"]), {}), stale_note=y.get("stale_note"),
        # raw Strategy API payloads travel with the snapshot so an off-network refresh (OCI) can reuse them
        _raw_permissions=y.get("raw_permissions"), _raw_best_strategy=y.get("raw_best_strategy"),
        _raw_strategy_current=(json.loads((run / "raw_strategy_current.json").read_text(encoding="utf-8"))
                               if (run / "raw_strategy_current.json").exists() else None),
        ops_tools_caveat=y.get("ops_tools_caveat"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default=str(ROOT / "runs"))
    ap.add_argument("--site", default=str(ROOT))
    ap.add_argument("--clients", nargs="*", default=None)
    a = ap.parse_args()
    reg = registry()
    slugs = a.clients or list(reg["clients"])
    runs, site = Path(a.runs), Path(a.site)
    index = dict(generated=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), clients=[])
    for slug in slugs:
        run = latest_run(runs, slug)
        if not run:
            print(f"[{slug}] no run found under {runs / slug}; skipped")
            continue
        snap = snapshot(slug, run)
        write_json(site / "data" / f"{slug}.json", snap)
        index["clients"].append(dict(client=slug, display_name=snap["display_name"], as_of=snap["as_of"],
                                     nav_usd=snap["nav_usd"], run_folder=run.name, file=f"data/{slug}.json"))
        print(f"[{slug}] {run.name}: NAV ${snap['nav_usd']:,.0f}, {len(snap['book'])} book rows, {len(snap['permitted'])} permitted venues -> data/{slug}.json")
    write_json(site / "data" / "index.json", index)
    print(f"DONE -> {site / 'data' / 'index.json'}")


if __name__ == "__main__":
    main()
