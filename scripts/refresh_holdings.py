"""Live-holdings refresh for the page, runnable from GitHub Actions.

    python refresh_holdings.py [--clients ens nexus cow balancer] [--out <dir>]

Pulls Syncrone v2, Safe Transaction Service and Etherscan V2 (with the public-RPC fallback) for
each client's avatar Safe on its default chain, reconciles Safe vs Etherscan per token, and writes
data/<client>.live.json: NAV, positions by protocol and asset group, idle, in-flight, token check
and flags. It does NOT call the KPK Strategy API (internal network only), so no APY here; the page
pairs this with the stored snapshot from the last local `run` for yields and permissions.

Needs SYNCRONE_API_KEY and ETHERSCAN_API_KEY (SAFE_API_KEY optional) in the environment.
Never prints key values. Exit code is 0 even when a client fails; the failure is written into the
client's live file so the page can show it.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import traceback
from pathlib import Path

from common import client, fnum, load_env, registry, write_json
from fetch_holdings import (DUST_USD, ZERO, collect_rewards, fetch_etherscan, fetch_safe_balances, fetch_syncrone,
                            rebase_idle_on_safe, reconcile, scope_syncrone, syncrone_rows)

ROOT = Path(__file__).resolve().parent.parent


def live_snapshot(slug: str, reg: dict) -> dict:
    c = client(slug)
    chain_id = c["chains"][0]
    safe = c["safes"][str(chain_id)]["avatar"]
    org = fetch_syncrone(c, reg)
    rows_all = syncrone_rows(org, reg) if org else []
    rows, other = scope_syncrone(rows_all, safe, chain_id)
    safe_rows = fetch_safe_balances(reg, chain_id, safe)
    lag, fresh = rebase_idle_on_safe(rows, safe_rows, reg, safe, chain_id)   # live Safe wins over Syncrone's lag
    tokens = {r["token"]: r["decimals"] for r in safe_rows}
    for r in rows:
        if r["token"] and r["token"] not in tokens and (r["kind"] == "position" or not r["spam"]):
            tokens[r["token"]] = 18
    eth_rows = fetch_etherscan(chain_id, safe, tokens)
    rewards = collect_rewards(c, reg, chain_id, safe, safe_rows, rows)
    held = {r["token"] for r in rewards if r["source"] == "wallet"}
    for r in rows:
        if r["kind"] == "idle" and r["token"] in held:
            r["asset_group"] = "REWARDS"
    recon, nav, flags = reconcile(rows, [], safe_rows, eth_rows, reg, c)
    aliases = reg["protocol_aliases"]
    by_sleeve: dict[str, dict] = {}
    for r in rows:
        if r["kind"] != "position" or r["usd"] < DUST_USD:
            continue
        proto = aliases.get(r["protocol"].lower(), r["protocol"].lower())
        k = f"{proto}/{r['asset_group']}"
        s = by_sleeve.setdefault(k, dict(protocol=proto, asset_group=r["asset_group"], usd=0.0, in_flight_usd=0.0, positions=set()))
        s["usd"] += r["usd"]
        if r.get("in_flight"):
            s["in_flight_usd"] += r["usd"]
        s["positions"].add(r["position"])
    sleeves = [dict(protocol=s["protocol"], asset_group=s["asset_group"], usd=round(s["usd"]), in_flight_usd=round(s["in_flight_usd"]),
                    positions=sorted(s["positions"])) for s in sorted(by_sleeve.values(), key=lambda s: -s["usd"])]
    idle = [dict(symbol=r["symbol"], balance=r["balance"], usd=round(r["usd"]), asset_group=r["asset_group"])
            for r in rows if r["kind"] == "idle" and not r["spam"] and r["usd"] >= DUST_USD]
    groups: dict[str, float] = {}
    for r in rows:
        if (r["kind"] == "position" or not r["spam"]) and r["usd"] >= DUST_USD:
            groups[r["asset_group"]] = groups.get(r["asset_group"], 0.0) + r["usd"]
    other_w: dict[str, float] = {}
    for r in other:
        if not r.get("spam"):
            k = f"{r['wallet']} chain {r.get('chain')}"
            other_w[k] = other_w.get(k, 0.0) + r["usd"]
    toks = recon["tokens"]
    return dict(
        client=slug, display_name=c["display_name"], chain_id=chain_id, avatar_safe=safe, ok=True,
        as_of=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), syncrone_window=org.get("_window") if org else None,
        nav_usd=nav["syncrone_nav_usd"], positions_usd=nav["syncrone_positions_usd"], idle_usd=nav["syncrone_idle_usd"],
        in_flight_usd=nav["syncrone_in_flight_usd"], onchain_idle_eth=nav["onchain_idle_eth"], eth_price_usd=nav["eth_price_usd"],
        groups={k: round(v) for k, v in groups.items()}, sleeves=sleeves, idle=idle,
        in_flight_items=nav["in_flight_items"],
        token_check=dict(checked=len(toks), disagree=sum(1 for t in toks if t["safe_vs_etherscan"] is not None and t["safe_vs_etherscan"] > 0.01),
                         etherscan_source=(eth_rows[1].get("explorer") if len(eth_rows) > 1 else "etherscan")),
        other_wallets={k: round(v) for k, v in other_w.items() if v > DUST_USD},
        unbooked=[dict(symbol=r["symbol"], balance=r["balance"], position=r["position"]) for r in fresh],
        rewards=[dict(source=r["source"], symbol=r["symbol"], amount=r["amount"], usd=round(r["usd"]), claimable=r["claimable"]) for r in rewards],
        rewards_usd=round(sum(r["usd"] for r in rewards)),
        flags=[f for f in flags if not f.startswith("NOTE: positions the Strategy API")]
              + ([f"NOTE: idle re-based on the live Safe (Syncrone lagging): {'; '.join(lag[:6])}"] if lag else [])
              + ([f"NOTE: Safe holds vault shares Syncrone has not booked yet: " + ", ".join(f"{r['balance']:,.2f} {r['symbol']}" for r in fresh)] if fresh else []))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clients", nargs="*", default=None)
    ap.add_argument("--out", default=str(ROOT / "data"))
    a = ap.parse_args()
    load_env()
    reg = registry()
    out = Path(a.out)
    for slug in a.clients or list(reg["clients"]):
        try:
            snap = live_snapshot(slug, reg)
            print(f"[{slug}] live NAV ${snap['nav_usd']:,.0f} | idle ${snap['idle_usd']:,.0f} | in-flight ${snap['in_flight_usd']:,.0f} | "
                  f"Safe=Etherscan {snap['token_check']['checked'] - snap['token_check']['disagree']}/{snap['token_check']['checked']} via {snap['token_check']['etherscan_source']}")
        except SystemExit as e:  # missing key
            snap = dict(client=slug, ok=False, as_of=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), error=str(e))
            print(f"[{slug}] FAILED: {e}")
        except Exception as e:
            snap = dict(client=slug, ok=False, as_of=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), error=f"{type(e).__name__}: {str(e)[:300]}")
            print(f"[{slug}] FAILED: {snap['error']}")
            traceback.print_exc(limit=2)
        write_json(out / f"{slug}.live.json", snap)
    print("DONE")


if __name__ == "__main__":
    sys.exit(main())
