"""Pull the permitted yield universe for a kpk client and the ops-tools optimizer's suggestion.

    python fetch_yields.py --client ens --out <dir> [--period 7day] [--chain 1] [--tvl-cap 10]

Sources:
  1. KPK Strategy API /clients/permissions   -> every protocol/asset/action the client's Roles
     permissions allow, enriched with vaults.fyi APY (1h/1day/7day/30day) and TVL where vaults.fyi
     tracks the venue. Entries with apy == null are permitted but unpriced (Spark, Balancer pools,
     Morpho markets, some Morpho vaults): the assessment lists them as "permitted, yield unknown".
  2. KPK Strategy API /clients/best-strategy -> the ops-tools optimizer output for reference.
     WARNING: it optimises APY under a per-venue TVL cap only. It ignores client policy caps
     (e.g. the ENS 30% single-protocol cap and the stablecoin floor) and excludes swap-only
     venues (ether.fi eETH) from the universe. Never forward it as the recommendation.
  3. vaults.fyi direct (optional, needs VAULTS_FYI_API_KEY): benchmark APY for USD and ETH, and
     APY for any permitted vault the Strategy API returned without APY.

Writes <out>/yields.json.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from common import client, fnum, http_json, key, load_env, registry, write_json

PERMITTED_ACTIONS = {"deposit", "stake", "swap"}


def fetch(reg, c, chain_id, period, tvl_cap):
    base = reg["strategy_api_base"]
    chain = reg["strategy_api_chain_names"][str(chain_id)]
    name = c["strategy_api_name"]
    perms = http_json(f"{base}/clients/permissions", data={"clientName": name, "chain": chain}, timeout=240)
    best = None
    try:
        best = http_json(f"{base}/clients/best-strategy",
                         data={"clientName": name, "period": period, "chain": chain,
                               "assetGroupConstraints": {}, "tvlCapPercent": tvl_cap}, timeout=300)
    except Exception as e:
        print("  best-strategy unavailable:", str(e)[:150])
    return perms, best


def flatten_permissions(perms: dict, period: str, reg: dict) -> list[dict]:
    from common import asset_group_of
    rows = []
    for proto, entries in (perms.get("permissions") or {}).items():
        for e in entries:
            apy = e.get("apy") or {}
            sel = apy.get(period) or {}
            vf = e.get("vaultsfyiInfo") or {}
            rows.append(dict(protocol=proto, asset=e.get("asset"), action=e.get("action"),
                             chain=e.get("chain"), vault=(vf.get("vault") or "").lower() or None,
                             apy_total=sel.get("total"), apy_base=sel.get("base"), apy_reward=sel.get("reward"),
                             apy_30d=(apy.get("30day") or {}).get("total"),
                             apy_all=apy or None,
                             tvl_usd=fnum((e.get("tvl") or {}).get("usd")) if e.get("tvl") else None,
                             asset_group=asset_group_of(e.get("asset") or "", reg),
                             priced=sel.get("total") is not None, apy_source="vaults.fyi" if sel.get("total") is not None else None,
                             sell_assets=e.get("sellAssets"), buy_assets=e.get("buyAssets")))
    return rows


def defillama_fallback(reg: dict) -> dict:
    """Keyless APYs for sleeves the Strategy API does not price, from yields.llama.fi/pools,
    keyed as clients.json `defillama_pools` says. Returns {key: {apy, apy_base, tvl_usd, pool, project, symbol}}."""
    table = {k: v for k, v in reg.get("defillama_pools", {}).items() if not k.startswith("_")}
    if not table:
        return {}
    try:
        pools = {p["pool"]: p for p in http_json("https://yields.llama.fi/pools", timeout=120)["data"]}
    except Exception as e:
        print("  defillama:", str(e)[:120])
        return {}
    out = {}
    for k, pid in table.items():
        p = pools.get(pid)
        if p:
            out[k] = dict(apy=fnum(p.get("apy")) / 100, apy_base=fnum(p.get("apyBase")) / 100 if p.get("apyBase") is not None else None,
                          tvl_usd=fnum(p.get("tvlUsd")), pool=pid, project=p["project"], symbol=p["symbol"], chain=p["chain"],
                          source="defillama")
    return out


LLAMA_PROJECT = {"aave_v3": "aave-v3", "compound_v3": "compound-v3", "fluid": "fluid-lending", "sky": "sky-lending",
                 "spark": "sparklend", "lido": "lido", "ether_fi": "ether.fi-stake", "stader": "stader", "stakewise_v3": "stakewise-v3",
                 "rocket_pool": "rocket-pool", "gearbox": "gearbox", "morphoVaults": "morpho-blue"}
LLAMA_CHAIN = {1: "Ethereum", 100: "Gnosis", 42161: "Arbitrum", 8453: "Base"}


def llama_symbol(protocol: str, asset: str) -> str | None:
    a = (asset or "").strip()
    if protocol == "morphoVaults":
        if not a.lower().startswith("kpk"):
            return None                              # non-kpk vaults: not resolvable by name
        words = [w for w in a.replace("-", " ").split() if not (w.lower().startswith("v") and w[1:].isdigit())]
        return "-".join(w.upper() for w in words)   # "kpk USDC Yield v2" -> KPK-USDC-YIELD
    if protocol == "compound_v3":
        au = a.upper()
        return au[1:-2] if au.startswith("C") and au.endswith("V3") else au
    if protocol == "sky" or (protocol == "spark" and a.upper() in ("USDS", "SUSDS")):
        return "SUSDS"
    if protocol == "ether_fi":
        return "WEETH"
    return {"eETH": "WEETH", "ETH": "WETH" if protocol in ("gearbox", "aave_v3", "spark") else "ETH"}.get(a, a.upper())


def live_apy_refresh(rows: list[dict], chain_id: int) -> tuple[int, int]:
    """Off-network alternative to vaults.fyi: match every permitted venue to a DeFiLlama pool
    (project + chain + symbol, highest TVL wins) and overwrite the cached APY/TVL. Returns
    (matched, unmatched). Rows that do not match keep their cached values and are labelled stale."""
    try:
        pools = http_json("https://yields.llama.fi/pools", timeout=120)["data"]
    except Exception as e:
        print("  defillama live refresh failed:", str(e)[:120])
        return 0, len(rows)
    chain = LLAMA_CHAIN.get(chain_id, "Ethereum")
    by_key: dict[tuple, list] = {}
    for p in pools:
        if p.get("chain") == chain:
            by_key.setdefault((p["project"], p["symbol"].upper()), []).append(p)
    matched = unmatched = 0
    for r in rows:
        proj = LLAMA_PROJECT.get(r["protocol"])
        sym = llama_symbol(r["protocol"], r.get("asset")) if proj else None
        if sym == "SUSDS":
            proj = "sky-lending"
        cands = [p for p in by_key.get((proj, sym), []) if fnum(p.get("apy")) > 0] if proj and sym else []
        if not cands:
            unmatched += 1
            if r.get("priced"):
                r["apy_source"] = f"{r.get('apy_source') or 'cache'} (stale)"
            continue
        p = max(cands, key=lambda x: fnum(x.get("tvlUsd")))
        r.update(apy_total=fnum(p.get("apy")) / 100, apy_base=(fnum(p.get("apyBase")) / 100) if p.get("apyBase") is not None else None,
                 apy_reward=(fnum(p.get("apyReward")) / 100) if p.get("apyReward") is not None else None,
                 apy_30d=(fnum(p.get("apyMean30d")) / 100) if p.get("apyMean30d") is not None else r.get("apy_30d"),
                 tvl_usd=fnum(p.get("tvlUsd")), priced=True, apy_source="defillama live", llama_pool=p["pool"])
        matched += 1
    return matched, unmatched


def vaults_fyi_benchmarks(chain_name: str) -> dict | None:
    k = key("VAULTS_FYI_API_KEY")
    if not k:
        return None
    out = {}
    for code in ("usd", "eth"):
        try:
            out[code] = http_json(f"https://api.vaults.fyi/v2/benchmarks?network={chain_name}&code={code}",
                                  headers={"x-api-key": k})
        except Exception as e:
            out[code] = {"error": str(e)[:150]}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--period", default="7day", choices=["1h", "1day", "7day", "30day"])
    ap.add_argument("--chain", type=int, default=None)
    ap.add_argument("--tvl-cap", type=float, default=10.0, help="ops-tools optimizer per-venue TVL cap %%")
    a = ap.parse_args()
    load_env()
    reg = registry()
    c = client(a.client)
    if not c.get("strategy_api_name"):
        sys.exit(f"ERROR: {a.client} has no Strategy API client; permissions must come from the Roles config.")
    chain_id = a.chain or c["chains"][0]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    stale_note = None
    try:
        perms, best = fetch(reg, c, chain_id, a.period, a.tvl_cap)
    except Exception as e:
        # Strategy API is office-network only. Off-network (OCI), reuse the permissions from the
        # last published snapshot; APYs there are as old as vault_data_fetched_at and are marked stale.
        prev = Path(__file__).resolve().parent.parent / "data" / f"{a.client}.strategy.json"
        if not prev.exists():
            sys.exit(f"ERROR: Strategy API unreachable ({str(e)[:100]}) and no cache at {prev}; run `run` on the office network first")
        pj = json.loads(prev.read_text(encoding="utf-8"))
        perms, best = pj["raw_permissions"], pj.get("raw_best_strategy")
        stale_note = f"Strategy API unreachable; permissions and vaults.fyi APYs reused from the office cache of {pj.get('vault_data_fetched_at')}"
        print("  " + stale_note)
    write_json(out / "raw_permissions.json", perms)
    if best:
        write_json(out / "raw_best_strategy.json", best)
    rows = flatten_permissions(perms, a.period, reg)
    if stale_note:
        m, u = live_apy_refresh(rows, chain_id)
        stale_note = (f"Strategy API unreachable: permissions from the office cache of {perms.get('vaultDataFetchedAt')}; "
                      f"APYs refreshed live from DeFiLlama for {m} venues" + (f", {u} kept cached (stale)" if u else ""))
        print("  " + stale_note)
    priced = [r for r in rows if r["priced"]]
    print(f"[{a.client}] permissions: {len(rows)} permitted entries across {len(perms.get('permissions') or {})} protocols; "
          f"{len(priced)} priced by vaults.fyi; vault data at {perms.get('vaultDataFetchedAt')}")
    for grp in ("USD", "ETH", "EURO"):
        top = sorted([r for r in priced if r["asset_group"] == grp], key=lambda r: -fnum(r["apy_total"]))[:6]
        if top:
            print(f"  {grp}: " + ", ".join(f"{r['protocol']}/{r['asset']} {fnum(r['apy_total'])*100:.2f}%" for r in top))
    unpriced = sorted({f"{r['protocol']}/{r['asset']}" for r in rows if not r['priced'] and r['action'] != 'swap'})
    if unpriced:
        print(f"  permitted but unpriced ({len(unpriced)}): {', '.join(unpriced[:12])}{' ...' if len(unpriced) > 12 else ''}")

    bench = vaults_fyi_benchmarks(reg["strategy_api_chain_names"][str(chain_id)])
    fallback = defillama_fallback(reg)
    if fallback:
        print(f"  defillama fallback: {len(fallback)} sleeves priced (" + ", ".join(f"{k} {v['apy']*100:.2f}%" for k, v in list(fallback.items())[:6]) + " ...)")
    write_json(out / "yields.json", dict(
        client=a.client, chain_id=chain_id, period=a.period,
        vault_data_fetched_at=perms.get("vaultDataFetchedAt"),
        permitted=rows, fallback_apy=fallback, stale_note=stale_note,
        raw_permissions=perms, raw_best_strategy=best,
        all_permissions_protocols=sorted((perms.get("allPermissions") or {}).keys()),
        ops_tools_best_strategy=best,
        ops_tools_caveat="Optimizer respects only tvlCapPercent per venue. It ignores policy caps/floors and swap-only venues. Reference only.",
        vaults_fyi_benchmarks=bench))
    print(f"DONE -> {out / 'yields.json'}")


if __name__ == "__main__":
    main()
