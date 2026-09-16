"""Fast lane: a snapshot-shaped view of one client rebuilt from live sources in a few seconds.

The full pipeline (run.py -> refresh_holdings -> publish -> git push) is the authoritative record and takes
1-3 minutes: Syncrone's month-to-date performance call, the whole DeFiLlama pool dump, sequential vaults.fyi
reads, rate-limited Etherscan reads and a git push. None of that is needed to answer "what does the Safe hold
right now and which moves still stand". This module answers that from:

  * Safe Transaction Service     token units held by the avatar Safe (1 call)
  * DeBank Pro                   every DeFi position with live USD and prices (2 calls)
  * vaults.fyi                   live APY/TVL per vault address, fetched in parallel (~30 calls, ~2 s)
  * on-chain reward reads        Merkl, Compound, Aave, Safety Module, Uniswap fees (collect_rewards)

Permissions, the Roles gate, the policy block and the ops-tools reference are taken from the last published
snapshot (data/<client>.json); they only change when a PUR lands or the office re-runs. The result is passed
through the same policy / performance / rewards-sweep functions the pipeline uses, so the page can swap it in
for the stored snapshot without knowing the difference. Executed moves disappear because the balances moved.
"""
from __future__ import annotations

import datetime as dt
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from common import asset_group_of, client, fnum, http_json, key, load_env, registry

ROOT = Path(__file__).resolve().parent.parent


def _fetch_positions(reg, c, chain_id, safe):
    from fetch_holdings import fetch_debank, fetch_safe_balances
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_safe = ex.submit(fetch_safe_balances, reg, chain_id, safe)
        f_deb = ex.submit(fetch_debank, chain_id, safe)
        return f_safe.result(), f_deb.result()


def _vault_apys(chain_id: int, addrs: list[str], period: str) -> dict:
    from fetch_yields import vaults_fyi_vault
    addrs = sorted({a.lower() for a in addrs if a})
    out = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for a, d in zip(addrs, ex.map(lambda a: vaults_fyi_vault(chain_id, a), addrs)):
            if d:
                out[a] = d
    return out


def _underlying(sym: str) -> str:
    """aEthUSDC -> USDC, weETH/eETH/wstETH/stETH/ETHx/osETH/rETH -> ETH, fUSDT -> USDT, sUSDS -> USDS, cUSDCv3 -> USDC."""
    s = (sym or "").upper()
    if s.startswith("KPK_") or s.startswith("KPK "):          # kpk vault shares: KPK_USDC_Prime -> USDC
        parts = s.replace(" ", "_").split("_")
        if len(parts) > 1:
            return _underlying(parts[1])
    for pre in ("AETH", "AARB", "WA", "F", "C", "S", "A"):
        if s.startswith(pre) and len(s) > len(pre) + 2 and s[len(pre):].rstrip("V3") in ("USDC", "USDT", "USDS", "DAI", "GHO", "EURC"):
            return s[len(pre):].rstrip("V3")
    if s in ("WEETH", "EETH", "WSTETH", "STETH", "ETHX", "OSETH", "RETH", "WETH", "ETH"):
        return "ETH"
    return s.rstrip("V3") if s.endswith("V3") else s


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

    safe_rows, debank = _fetch_positions(reg, c, chain_id, safe)
    if not debank:
        raise RuntimeError("DeBank unavailable (DEBANK_ACCESS_KEY missing or API down); the fast lane needs it for live position values")

    # ---- live prices per token / symbol (DeBank wallet list + supply tokens inside positions)
    price_of, price_sym = {}, {}
    for w in debank.get("wallet", []):
        if w.get("price"):
            price_of[w["token"]] = w["price"]; price_sym[(w.get("symbol") or "").upper()] = w["price"]
    raw_protos = debank.get("protocols") or {}

    # ---- DeBank positions indexed for matching: by pool id (receipt/vault address) and by (protocol, underlying)
    items = []
    for did, p in raw_protos.items():
        ours = norm_proto(p.get("ours") or did)
        if ours == "merkl":
            continue                                  # claimables come from the on-chain rewards collector below
        for it in p.get("items", []):
            toks = [t for t in it.get("tokens", []) if t and t[0]]
            items.append(dict(protocol=ours, name=it.get("name"), usd=fnum(it.get("usd")), tokens=toks,
                              underlying={_underlying(t[0]) for t in toks}, pool=(it.get("pool") or "").lower()))
    # fetch_debank does not keep pool ids; recover them cheaply from the raw protocol payload when present
    # (items carry 'pool' only if fetch_debank was extended; matching still works on protocol+underlying)

    # ---- rebase the stored book on live values
    base_book = [dict(b) for b in base.get("book", []) if b["kind"] in ("position", "in_flight")]
    used = set()
    def take_all(pred):
        """Every unmatched DeBank item satisfying pred, summed. DeBank lists e.g. a zero-value 'Rewards' item beside the
        real 'Staked' one for the same protocol; the position is the sum, not the first hit."""
        hits = [i for i, it in enumerate(items) if i not in used and pred(it)]
        if not hits:
            return None
        used.update(hits)
        return dict(usd=sum(items[i]["usd"] for i in hits), n=len(hits))
    book = []
    for b in base_book:
        if norm_proto(b["protocol"]) == "merkl":
            continue
        proto = norm_proto(b["protocol"]); und = _underlying(b.get("symbol")); vault = (b.get("vault") or "").lower()
        hit = take_all(lambda it: it["protocol"] == proto and vault and it["pool"] == vault) if vault else None
        if not hit:
            hit = take_all(lambda it: it["protocol"] == proto and und in it["underlying"])
        if not hit and sum(1 for x in base_book if norm_proto(x["protocol"]) == proto) == 1:
            hit = take_all(lambda it: it["protocol"] == proto)      # one stored position, one protocol: whatever DeBank has is it
        if hit and hit["usd"] > 0:
            b["usd"] = round(hit["usd"], 2); b["live"] = "debank"
            book.append(b)
        elif hit:
            continue                                                 # DeBank sees the venue but nothing in it: exited
        else:
            # nothing live for this venue: the position was exited (or DeBank does not index it). Drop it when the
            # protocol is otherwise seen by DeBank, keep it flagged when the protocol is not indexed at all.
            if any(it["protocol"] == proto for it in items):
                continue
            b["live"] = "stale (not indexed by DeBank)"; book.append(b)
    # positions DeBank sees that the stored book did not have (new deposits): add as untracked, priced if a permitted vault matches
    permitted = [dict(p) for p in base.get("permitted", [])]
    for i, it in enumerate(items):
        if i in used or it["usd"] < 1000 or it["protocol"] in ("merkl", "(wallet)"):
            continue
        grp = "ETH" if "ETH" in it["underlying"] else ("USD" if it["underlying"] & {"USDC", "USDT", "USDS", "DAI", "GHO"} else "OTHER")
        sym = it["tokens"][0][0] if it["tokens"] else "?"
        book.append(dict(kind="position", protocol=it["protocol"], venue=f"{it['protocol']} {it['name']} [{sym}] (new since last run)",
                         symbol=sym, asset_group=grp, usd=round(it["usd"], 2), apy=None, apy_source=None, untracked=True, live="debank (new)"))
    # idle: live Safe units x live prices
    known = {r["token"] for r in book if r.get("token")}
    receipt = reg.get("receipt_tokens", {})
    for sb in safe_rows:
        if sb["balance"] <= 0 or sb["token"] in receipt:
            continue
        px = price_of.get(sb["token"]) or price_sym.get((sb["symbol"] or "").upper())
        if not px:
            continue
        usd = sb["balance"] * px
        if usd < reg.get("spam_dust_usd", 50):
            continue
        grp = asset_group_of(sb["symbol"], reg)
        if grp == "OTHER" and (sb["symbol"] or "").upper() not in {s.upper() for s in (reg.get("asset_groups", {}).get("OTHER") or [])}:
            continue   # unknown token: spam guard, the Safe list is full of airdropped junk
        book.append(dict(kind="idle", protocol="(wallet)", venue=f"idle in Safe [{sb['symbol']}]", symbol=sb["symbol"], asset_group=grp,
                         usd=round(usd, 2), apy=0.0, apy_source=None, balance=sb["balance"], token=sb["token"], live="safe"))

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

    # ---- rewards: same collector as the pipeline, priced from DeBank
    from fetch_holdings import collect_rewards
    price_rows = [dict(token=t, price=px, symbol=next((s for s, v in price_sym.items() if v == px), None), kind="idle") for t, px in price_of.items()]
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

    snap = dict(base)
    snap.update(
        live=True, live_as_of=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), live_seconds=round(time.time() - t0, 1),
        live_sources=dict(positions="DeBank", units="Safe Transaction Service", apys=f"vaults.fyi ({len(va)} vaults)", rewards="on-chain"),
        base_as_of=base.get("as_of"), base_run=base.get("run_folder"),
        nav_usd=round(nav), eth_price_usd=price_sym.get("ETH") or base.get("eth_price_usd"),
        book=[dict(kind=b["kind"], protocol=b["protocol"], venue=b["venue"], symbol=b.get("symbol"), asset_group=b["asset_group"],
                   usd=round(fnum(b["usd"]), 2), apy=b.get("apy"), apy_source=b.get("apy_source"), venue_tvl_usd=b.get("venue_tvl_usd"),
                   untracked=b.get("untracked", False), balance=b.get("balance"), claimable=b.get("claimable", False),
                   claim_cmd=b.get("claim_cmd"), vault=b.get("vault"), live=b.get("live")) for b in book if fnum(b["usd"]) >= 1],
        permitted=permitted, policy_checks=checks, rewards_sweep=sweep,
        apy_sources={k: sum(1 for p in permitted if (p.get("apy_source") or "none") == k) for k in sorted({(p.get("apy_source") or "none") for p in permitted})},
        vault_apys_live=len(va),
        debank=dict(nav_usd=debank["nav_usd"], protocol_nav_usd=debank["protocol_nav_usd"], wallet_usd=debank["wallet_usd"],
                    protocols={v["ours"]: round(v["usd"]) for v in raw_protos.values() if v["usd"] > 1000}, diff_vs_syncrone=None, notes=[]),
        reconciliation=dict(base.get("reconciliation") or {}, live_note=f"live view: DeBank NAV ${debank['nav_usd']:,.0f} vs stored snapshot ${base.get('nav_usd', 0):,.0f}"),
        flags=[f"LIVE VIEW ({dt.datetime.now(dt.timezone.utc).strftime('%H:%M:%S')} UTC): positions from DeBank, units from the Safe, APYs from vaults.fyi. "
               f"Permissions and policy from the stored snapshot of {base.get('as_of')}. Run a full refresh for the audited reconciliation."]
              + [f for f in (base.get("flags") or []) if f.startswith("NOTE")][:3],
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
