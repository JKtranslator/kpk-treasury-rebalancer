"""Fast lane: a snapshot-shaped view of one client rebuilt from free live sources in a few seconds.

The full pipeline (run.py -> refresh_holdings -> publish -> git push) is the authoritative record and takes
1-3 minutes: Syncrone's month-to-date performance call, the whole DeFiLlama pool dump, sequential vaults.fyi
reads, rate-limited Etherscan reads and a git push. None of that is needed to answer "what does the Safe hold
right now and which moves still stand". This module answers that from:

  * last published snapshot     Syncrone is the base: per position the receipt token the Safe holds and the
                                USD mark of one unit at the run (scripts/receipts.py), plus the run's prices
  * Safe Transaction Service    token units held by the avatar Safe now (1 call, free)
  * public JSON-RPC             Chainlink ETH/USD for the ETH sleeve; getShares() for share-only vaults (free)
  * vaults.fyi                  live APY/TVL per vault address, fetched in parallel (~30 calls, 3 CU each)
  * on-chain reward reads       Merkl, Compound, Aave, Safety Module, Uniswap fees (collect_rewards)

A position is `Safe units now x unit mark at run`, re-priced for ETH from Chainlink; a receipt balance that
went to zero is an exited position and drops out; a new receipt token in the Safe is flagged for a full
refresh. Permissions, the Roles gate, the policy block and the ops-tools reference are taken from the last
published snapshot (data/<client>.json). The result passes through the same policy / performance /
rewards-sweep functions the pipeline uses, so the page can swap it in for the stored snapshot.
"""
from __future__ import annotations

import datetime as dt
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from common import asset_group_of, client, fnum, key, load_env, registry
from receipts import CHAINLINK_ETH_USD, SEL_GET_SHARES, SEL_LATEST_ANSWER

ROOT = Path(__file__).resolve().parent.parent
STABLE_SYMS = {"USDC", "USDT", "USDS", "DAI", "GHO", "EURC", "PYUSD", "RLUSD"}


def _eth_price(chain_id: int, fallback: float | None) -> tuple[float | None, str]:
    from fetch_holdings import _rpc_call
    feed = CHAINLINK_ETH_USD.get(chain_id)
    if not feed:
        return fallback, "snapshot"
    try:
        px = int(_rpc_call(chain_id, feed, SEL_LATEST_ANSWER), 16) / 1e8
        return (px, "chainlink") if px > 0 else (fallback, "snapshot")
    except Exception as e:
        print("  chainlink:", str(e)[:100])
        return fallback, "snapshot"


def _shares(chain_id: int, vault: str, safe: str) -> float:
    from fetch_holdings import _rpc_call
    return int(_rpc_call(chain_id, vault, SEL_GET_SHARES + safe[2:].lower().rjust(64, "0")), 16) / 1e18


def _vault_apys(chain_id: int, addrs: list[str], period: str) -> dict:
    from fetch_yields import vaults_fyi_vault
    addrs = sorted({a.lower() for a in addrs if a})
    out = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for a, d in zip(addrs, ex.map(lambda a: vaults_fyi_vault(chain_id, a), addrs)):
            if d:
                out[a] = d
    return out


def live_snapshot(slug: str, base: dict | None = None) -> dict:
    """Return a snapshot-shaped dict for the page, built from live sources on top of the last published snapshot."""
    t0 = time.time()
    load_env()
    reg = registry()
    c = client(slug)
    if base is None:
        base = json.loads((ROOT / "data" / f"{slug}.json").read_text(encoding="utf-8"))
    chain_id = int(base.get("chain_id") or c["chains"][0])
    safe = (base.get("safes") or {}).get("avatar") or c["safes"][str(chain_id)]["avatar"]
    period = base.get("period") or "7day"
    aliases = reg.get("protocol_aliases", {})
    norm_proto = lambda p: aliases.get((p or "").lower(), (p or "").lower())

    from fetch_holdings import fetch_safe_balances
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_safe = ex.submit(fetch_safe_balances, reg, chain_id, safe)
        f_eth = ex.submit(_eth_price, chain_id, fnum(base.get("eth_price_usd")) or None)
        safe_rows = f_safe.result()
        eth_live, eth_src = f_eth.result()
    if not safe_rows:
        raise RuntimeError("Safe Transaction Service returned no balances; the fast lane needs them")
    held = {r["token"]: fnum(r["balance"]) for r in safe_rows if fnum(r["balance"]) > 0}
    eth_base = fnum(base.get("eth_price_usd")) or None
    eth_factor = (eth_live / eth_base) if (eth_live and eth_base) else 1.0
    factor = lambda grp: eth_factor if grp == "ETH" else 1.0
    prices = base.get("prices") or {}
    notes = []

    # ---- positions: Safe units now x unit mark at run
    book, known_receipts = [], set()
    base_book = [dict(b) for b in base.get("book", []) if b["kind"] in ("position", "in_flight")]
    for b in base_book:
        if norm_proto(b["protocol"]) == "merkl":
            continue                                                 # claimables come from the on-chain rewards collector below
        if b["kind"] == "in_flight":
            b["live"] = "stored (withdrawal queue)"; book.append(b); continue
        rc, unit = (b.get("receipt") or "").lower(), fnum(b.get("unit_usd"))
        if not rc or unit <= 0:
            b["live"] = "stale: no receipt token resolved at the run (full refresh to re-mark)"; book.append(b); continue
        known_receipts.add(rc)
        try:
            units = _shares(chain_id, rc, safe) if b.get("receipt_method") == "shares" else held.get(rc, 0.0)
        except Exception as e:
            print("  getShares:", str(e)[:100]); b["live"] = "stale: on-chain share read failed"; book.append(b); continue
        if units <= 0:
            continue                                                 # receipt gone: the position was exited
        b["usd"] = round(units * unit * factor(b.get("asset_group")), 2); b["balance"] = units
        b["live"] = "safe units x run mark" + (" x chainlink ETH" if b.get("asset_group") == "ETH" and eth_src == "chainlink" else "")
        book.append(b)
    # receipt tokens in the Safe the run's book did not have: new deposits, unvalued until a full refresh
    receipts_reg = reg.get("receipt_tokens", {})
    vaults_perm = {(p.get("vault") or "").lower(): p for p in base.get("permitted", []) if p.get("vault")}
    for t, units in held.items():
        if t in known_receipts or t in prices or units < 0.01:      # dust of a vault token beside the real receipt is not a deposit
            continue
        lab = receipts_reg.get(t) or (vaults_perm.get(t) and dict(protocol=vaults_perm[t]["protocol"], symbol=vaults_perm[t]["asset"]))
        if lab:
            notes.append(f"NOTE: the Safe holds {units:,.4f} {lab.get('symbol')} ({lab.get('protocol')}) that the last run did not book; "
                         f"value shown after a full refresh")

    # ---- idle: live Safe units x run prices (ETH sleeve re-priced)
    for sb in safe_rows:
        t, bal = sb["token"], fnum(sb["balance"])
        if bal <= 0 or t in known_receipts or t in receipts_reg:
            continue
        sym = sb.get("symbol") or ""
        pr = prices.get(t)
        if pr:
            px = pr["price"] * factor(pr.get("asset_group") or asset_group_of(sym, reg))
        elif sym.upper() == "ETH" and eth_live:
            px = eth_live
        elif sym.upper() in STABLE_SYMS:
            px = 1.0
        else:
            continue                                                 # unknown token, unknown price: spam guard
        usd = bal * px
        if usd < reg.get("spam_dust_usd", 50):
            continue
        grp = asset_group_of(sym, reg)
        if grp == "OTHER" and sym.upper() not in {s.upper() for s in (reg.get("asset_groups", {}).get("OTHER") or [])}:
            continue
        book.append(dict(kind="idle", protocol="(wallet)", venue=f"idle in Safe [{sym}]", symbol=sym, asset_group=grp,
                         usd=round(usd, 2), apy=0.0, apy_source=None, balance=bal, token=t, live="safe"))

    # ---- permitted venues: never reprice or propose what the on-chain Roles gate excluded
    permitted = [dict(p) for p in base.get("permitted", [])]
    gated = {(e.get("vault") or "").lower() for e in (base.get("roles_gate") or {}).get("excluded", []) if e.get("vault")}
    for p in permitted:
        if (p.get("vault") or "").lower() in gated or p.get("apy_source") == "NOT IN ROLES":
            p.update(in_roles=False, priced=False, apy_source="NOT IN ROLES")

    # ---- APYs: one parallel vaults.fyi pass over every vault we hold or may deposit into
    addrs = [b.get("vault") for b in book if b.get("vault")] + [p.get("vault") for p in permitted if p.get("vault") and p.get("in_roles") is not False]
    va = _vault_apys(chain_id, addrs, period)
    per = {"1h": "apy_1h", "1day": "apy_1d", "7day": "apy_7d", "30day": "apy_30d"}.get(period, "apy_7d")
    for b in book:
        d = va.get((b.get("vault") or "").lower())
        if d and d.get(per) is not None:
            b["apy"], b["apy_source"], b["venue_tvl_usd"] = d[per], "vaults.fyi live", d.get("tvl_usd") or b.get("venue_tvl_usd")
    for p in permitted:
        d = va.get((p.get("vault") or "").lower())
        if d and d.get(per) is not None and p.get("in_roles") is not False:
            p.update(apy=d[per], apy_total=d[per], apy_1d=d["apy_1d"], apy_30d=d["apy_30d"], tvl_usd=d.get("tvl_usd") or p.get("tvl_usd"),
                     priced=True, apy_source="vaults.fyi live")
        p.setdefault("apy_total", p.get("apy"))

    # ---- rewards: same collector as the pipeline, priced from the run's marks
    from fetch_holdings import collect_rewards
    price_rows = [dict(token=t, price=v["price"] * factor(v.get("asset_group")), symbol=v.get("symbol"), kind="idle") for t, v in prices.items()]
    try:
        rewards = collect_rewards(c, reg, chain_id, safe, safe_rows, price_rows)
    except Exception as e:
        rewards = []; print("  live rewards:", str(e)[:100])
    for r in rewards:
        if r["usd"] >= 1 and r["claimable"]:
            book.append(dict(kind="reward", protocol=r["source"], venue=f"claimable {r['symbol']} ({r.get('note') or r['source']})", symbol=r["symbol"],
                             asset_group="REWARDS", usd=r["usd"], apy=None, balance=r["amount"], claimable=True, claim_cmd=r.get("claim_cmd"),
                             claim_only=r.get("claim_only", False), token_id=r.get("token_id"), live="on-chain"))

    # ---- same engines as the pipeline
    from assess import performance, policy_checks, rewards_sweep
    nav = sum(b["usd"] for b in book)
    th = base.get("thresholds") or {}
    args = SimpleNamespace(min_pickup_bps=th.get("min_pickup_bps", 50), min_move_usd=th.get("min_move_usd", 250_000),
                           venue_tvl_cap_pct=th.get("venue_tvl_cap_pct", 10), exclude=th.get("exclude") or [])
    checks = policy_checks(book, nav, c.get("policy"), reg)
    perf = performance(book, permitted, nav, c.get("policy"), args, reg)
    sweep = rewards_sweep(book, permitted, reg)

    stale = sum(1 for b in book if str(b.get("live", "")).startswith("stale"))
    eth_line = (f"ETH ${eth_live:,.0f} ({eth_src}) vs ${eth_base:,.0f} at the run" if eth_live and eth_base else "ETH price from the run")
    snap = dict(base)
    snap.update(
        live=True, live_as_of=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), live_seconds=round(time.time() - t0, 1),
        live_sources=dict(positions="Safe units x run marks", units="Safe Transaction Service", eth_price=eth_src,
                          apys=f"vaults.fyi ({len(va)} vaults)", rewards="on-chain"),
        base_as_of=base.get("as_of"), base_run=base.get("run_folder"),
        nav_usd=round(nav), eth_price_usd=eth_live or eth_base,
        book=[dict(kind=b["kind"], protocol=b["protocol"], venue=b["venue"], symbol=b.get("symbol"), asset_group=b["asset_group"],
                   usd=round(fnum(b["usd"]), 2), apy=b.get("apy"), apy_source=b.get("apy_source"), venue_tvl_usd=b.get("venue_tvl_usd"),
                   untracked=b.get("untracked", False), balance=b.get("balance"), claimable=b.get("claimable", False),
                   claim_cmd=b.get("claim_cmd"), vault=b.get("vault"), receipt=b.get("receipt"), unit_usd=b.get("unit_usd"),
                   live=b.get("live")) for b in book if fnum(b["usd"]) >= 1],
        permitted=permitted, policy_checks=checks, rewards_sweep=sweep,
        apy_sources={k: sum(1 for p in permitted if (p.get("apy_source") or "none") == k) for k in sorted({(p.get("apy_source") or "none") for p in permitted})},
        vault_apys_live=len(va), debank=None,
        reconciliation=dict(base.get("reconciliation") or {},
                            live_note=f"live view: Safe units x run marks NAV ${nav:,.0f} vs stored snapshot ${base.get('nav_usd', 0):,.0f}; {eth_line}"
                                      + (f"; {stale} position(s) kept at the stored value" if stale else "")),
        flags=[f"LIVE VIEW ({dt.datetime.now(dt.timezone.utc).strftime('%H:%M:%S')} UTC): units from the Safe, marks from the run of {base.get('as_of')}, "
               f"{eth_line}, APYs from vaults.fyi. Permissions and policy from the stored snapshot. Run a full refresh for the audited reconciliation."]
              + notes + [f for f in (base.get("flags") or []) if f.startswith("NOTE")][:3],
        performance_summary={g: dict(pickup_usd_per_year=v.get("pickup_usd_per_year"), moves=len(v.get("candidate_moves") or [])) for g, v in perf.items()},
    )
    return snap


if __name__ == "__main__":
    import argparse, sys
    ap = argparse.ArgumentParser(); ap.add_argument("--client", required=True); ap.add_argument("--out", default=None)
    a = ap.parse_args()
    s = live_snapshot(a.client)
    if a.out:
        Path(a.out).write_text(json.dumps(s, indent=1, default=str), encoding="utf-8")
    print(f"[{a.client}] live NAV ${s['nav_usd']:,.0f} in {s['live_seconds']}s · {len(s['book'])} book rows · "
          f"{sum(v['moves'] for v in s['performance_summary'].values())} candidate moves · rewards ${s['rewards_sweep']['total_usd']:,}")
    sys.exit(0)
