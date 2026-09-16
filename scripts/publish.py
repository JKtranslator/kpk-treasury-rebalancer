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
                 untracked=b.get("untracked", False), balance=b.get("balance"), claimable=b.get("claimable", False),
                 claim_cmd=b.get("claim_cmd"), vault=b.get("vault")) for b in a["book"] if fnum(b["usd"]) >= 1]
    permitted = [dict(protocol=p["protocol"], asset=p["asset"], action=p["action"], asset_group=p["asset_group"],
                      apy=p.get("apy_total"), apy_30d=p.get("apy_30d"), apy_1d=p.get("apy_1d"), tvl_usd=p.get("tvl_usd"), priced=p["priced"],
                      vault=p.get("vault"), apy_source=p.get("apy_source"), in_roles=p.get("in_roles"), roles_note=p.get("roles_note"))
                 for p in y.get("permitted", []) if p["action"] in ("deposit", "stake", "swap")]
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
        rewards_sweep=a.get("rewards_sweep"),
        safes=c["safes"].get(str(h["chain_id"]), {}), stale_note=y.get("stale_note"),
        ops_tools_caveat=y.get("ops_tools_caveat"),
        # source provenance for the page: which venues the on-chain Roles gate excluded, how many APYs are live
        roles_gate=y.get("roles_gate"),
        apy_sources={k: sum(1 for p in permitted if (p.get("apy_source") or "none") == k)
                     for k in sorted({(p.get("apy_source") or "none") for p in permitted})},
        vault_apys_live=len(y.get("vault_apys") or {}),
        debank=(lambda d: dict(nav_usd=d["nav_usd"], protocol_nav_usd=d["protocol_nav_usd"], wallet_usd=d["wallet_usd"],
                               protocols={v["ours"]: round(v["usd"]) for v in d["protocols"].values() if v["usd"] > 1000},
                               diff_vs_syncrone=n.get("syncrone_vs_debank_diff"),
                               notes=[f for f in h["flags"] if f.startswith("NOTE (DeBank")]))(h["debank"]) if h.get("debank") else None)


def strategy_cache(slug: str, run: Path) -> dict | None:
    """Office-network artefact: the raw Strategy API payloads (permissions, positions, optimizer) the
    off-network executor reuses. Written only by a local `run`; the box never writes this file."""
    y = json.loads((run / "yields.json").read_text(encoding="utf-8"))
    if y.get("stale_note") or not y.get("raw_permissions"):
        return None
    cur = run / "raw_strategy_current.json"
    return dict(client=slug, cached_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                vault_data_fetched_at=y.get("vault_data_fetched_at"), period=y.get("period"), chain_id=y.get("chain_id"),
                raw_permissions=y["raw_permissions"], raw_best_strategy=y.get("raw_best_strategy"),
                raw_strategy_current=json.loads(cur.read_text(encoding="utf-8")) if cur.exists() else None)


def bump_asset_version(site: Path) -> None:
    """Refresh ?v= on the page's asset links so browsers pick up a new deploy immediately."""
    import re, time
    p = site / "index.html"
    if p.exists():
        v = str(int(time.time()))
        t = re.sub(r'assets/(app\.js|rebalancer\.css|styles\.css)(\?v=\d+)?', lambda m: f"assets/{m.group(1)}?v={v}", p.read_text(encoding="utf-8"))
        p.write_text(t, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default=str(ROOT / "runs"))
    ap.add_argument("--site", default=str(ROOT))
    ap.add_argument("--clients", nargs="*", default=None)
    ap.add_argument("--strategy-only", action="store_true",
                    help="write only data/<client>.strategy.json (office network); the box writes the snapshots")
    a = ap.parse_args()
    reg = registry()
    slugs = a.clients or list(reg["clients"])
    runs, site = Path(a.runs), Path(a.site)
    if a.strategy_only:
        for slug in slugs:
            run = latest_run(runs, slug)
            sc = strategy_cache(slug, run) if run else None
            if not sc:
                print(f"[{slug}] no fresh Strategy API payload in {run}; skipped")
                continue
            write_json(site / "data" / f"{slug}.strategy.json", sc)
            print(f"[{slug}] strategy cache {sc['vault_data_fetched_at']} -> data/{slug}.strategy.json")
        return
    # merge into the existing index so publishing one client keeps the others' entries
    idx_path = site / "data" / "index.json"
    existing = {}
    if idx_path.exists():
        try:
            existing = {c["client"]: c for c in json.loads(idx_path.read_text(encoding="utf-8")).get("clients", [])}
        except Exception:
            existing = {}
    for slug in slugs:
        run = latest_run(runs, slug)
        if not run:
            print(f"[{slug}] no run found under {runs / slug}; skipped")
            continue
        snap = snapshot(slug, run)
        write_json(site / "data" / f"{slug}.json", snap)
        existing[slug] = dict(client=slug, display_name=snap["display_name"], as_of=snap["as_of"],
                              nav_usd=snap["nav_usd"], run_folder=run.name, file=f"data/{slug}.json")
        print(f"[{slug}] {run.name}: NAV ${snap['nav_usd']:,.0f}, {len(snap['book'])} book rows, {len(snap['permitted'])} permitted venues -> data/{slug}.json")
    order = list(reg["clients"])
    index = dict(generated=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                 clients=[existing[s] for s in order if s in existing] + [v for k, v in existing.items() if k not in order])
    write_json(idx_path, index)
    print(f"DONE -> {site / 'data' / 'index.json'}")


if __name__ == "__main__":
    main()
