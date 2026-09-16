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
import re
import json
import sys
from pathlib import Path

from common import client, fnum, http_json, key, load_env, registry, roles_targets, write_json

VF_NETWORK = {1: "mainnet", 100: "gnosis", 42161: "arbitrum", 8453: "base", 10: "optimism"}
# ERC-4626-style venues where the vault address IS the Roles target the bot calls. For pool-style protocols
# (Aave/Spark lending, Sky) the Strategy API's vault field is the receipt token, not the target, so the gate is
# protocol-level there and the asset list is trusted from the API.
VAULT_IS_TARGET = {"morphoVaults", "fluid", "compound_v3", "gearbox", "ether_fi", "stakewise_v3", "stader", "lido", "rocket_pool"}


def roles_gate(rows: list[dict], targets: set[str] | None) -> list[dict]:
    """Mark every permitted venue with whether its deposit target is in the on-chain Roles. Venues that are not
    are kept in the list for visibility but unpriced, so neither engine can propose them."""
    if targets is None:
        for r in rows:
            r["in_roles"] = None
        return []
    gated = []
    for r in rows:
        v = r.get("vault")
        if v and r["protocol"] in VAULT_IS_TARGET:
            r["in_roles"] = v.lower() in targets
            if not r["in_roles"]:
                r.update(priced=False, apy_source="NOT IN ROLES", roles_note="listed by the Strategy API but the vault is not a target in the on-chain Roles (pending PUR?)")
                gated.append(r)
        else:
            r["in_roles"] = None   # protocol-level permission; the target is the pool/manager, not this address
    return gated


_VF_CACHE: dict[str, dict] = {}


def vaults_fyi_vault(chain_id: int, addr: str) -> dict | None:
    """Live APY/TVL for one vault straight from vaults.fyi (needs VAULTS_FYI_API_KEY). Works off-network."""
    k = key("VAULTS_FYI_API_KEY")
    net = VF_NETWORK.get(chain_id)
    if not k or not net or not addr:
        return None
    ck = f"{net}:{addr.lower()}"
    if ck in _VF_CACHE:
        return _VF_CACHE[ck]
    try:
        d = http_json(f"https://api.vaults.fyi/v2/detailed-vaults/{net}/{addr}", headers={"x-api-key": k}, timeout=60)
    except Exception as e:
        _VF_CACHE[ck] = None
        print(f"  vaults.fyi {addr[:10]}: {str(e)[:90]}")
        return None
    # A vault denominated in a yield-bearing asset quotes its own rate in that asset: Gearbox's wstETH market pays
    # 0.67% *in wstETH*, on top of the 2.28% the wstETH is already earning from staking. vaults.fyi carries both --
    # `apy` is the vault's own rate, `apyComposite.totalApy` the return measured in the group's base asset (ETH,
    # USD), intrinsic and vault rate compounded. The composite is the number comparable to everything else in the
    # same asset group, so it wins whenever the API publishes one (it is null for WETH/ETH/USDC-denominated vaults).
    comp = (d.get("apyComposite") or {}).get("totalApy")
    apy = comp or d.get("apy") or {}
    g = lambda w, f="total": (apy.get(w) or {}).get(f)
    intr = (d.get("apyComposite") or {}).get("intrinsicApy") or {}
    out = dict(apy_1h=g("1hour"), apy_1d=g("1day"), apy_7d=g("7day"), apy_30d=g("30day"),
               apy_base_7d=g("7day", "base"), apy_reward_7d=g("7day", "reward"),
               composite=bool(comp), intrinsic_7d=(intr.get("7day") or {}).get("total"),
               vault_own_7d=((d.get("apy") or {}).get("7day") or {}).get("total"),
               denom=(d.get("asset") or {}).get("symbol"),
               tvl_usd=fnum((d.get("tvl") or {}).get("usd")), name=d.get("name"),
               is_transactional=d.get("isTransactional"), remaining_capacity=d.get("remainingCapacity"),
               max_capacity=d.get("maxCapacity"), updated=d.get("lastUpdateTimestamp"), warnings=d.get("warnings"))
    _VF_CACHE[ck] = out
    return out


def vaults_fyi_refresh(rows: list[dict], chain_id: int, period: str) -> tuple[int, dict]:
    """First tier: overwrite every permitted venue's APY/TVL with a live vaults.fyi read, keyed by vault address.
    Returns (count, {vault: data}); the map is also applied to book positions in assess.py."""
    per = {"1h": "apy_1h", "1day": "apy_1d", "7day": "apy_7d", "30day": "apy_30d"}[period]
    vault_apys, n = {}, 0
    for r in rows:
        if not r.get("vault") or r.get("in_roles") is False:
            continue
        d = vaults_fyi_vault(chain_id, r["vault"])
        if not d or d.get(per) is None:
            continue
        vault_apys[r["vault"].lower()] = d
        r.update(apy_total=d[per], apy_1d=d["apy_1d"], apy_30d=d["apy_30d"], apy_1h=d["apy_1h"],
                 apy_base=d["apy_base_7d"] if period == "7day" else r.get("apy_base"),
                 tvl_usd=d["tvl_usd"] if d["tvl_usd"] else r.get("tvl_usd"),
                 priced=True, apy_source="vaults.fyi live", vf_updated=d["updated"],
                 apy_intrinsic_lst=d.get("intrinsic_7d") if d.get("composite") else None,
                 apy_vault_own=d.get("vault_own_7d") if d.get("composite") else None, apy_denom=d.get("denom"))
        n += 1
    return n, vault_apys

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
                             apy_1d=(apy.get("1day") or {}).get("total"), apy_1h=(apy.get("1h") or {}).get("total"),
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
    by_key = {}
    for p in pools.values():
        if p.get("chain") == "Ethereum":
            by_key.setdefault((p["project"], p["symbol"].upper()), []).append(p)
    lst = lst_yields(by_key)
    for k, pid in table.items():
        p = pools.get(pid)
        if p:
            proto = k.split("/")[0]
            intrinsic = lst.get(p["symbol"].upper(), 0.0) if proto in LENDING_ON_LST and p["symbol"].upper() in LST_BASE else 0.0
            out[k] = dict(apy=fnum(p.get("apy")) / 100 + intrinsic, apy_base=(fnum(p.get("apyBase")) / 100 + intrinsic) if p.get("apyBase") is not None else None,
                          apy_intrinsic_lst=intrinsic or None,
                          tvl_usd=fnum(p.get("tvlUsd")), pool=pid, project=p["project"], symbol=p["symbol"], chain=p["chain"],
                          source="defillama")
    return out


LLAMA_PROJECT = {"aave_v3": "aave-v3", "compound_v3": "compound-v3", "fluid": "fluid-lending", "sky": "sky-lending",
                 "spark": "sparklend", "lido": "lido", "ether_fi": "ether.fi-stake", "stader": "stader", "stakewise_v3": "stakewise-v3",
                 "rocket_pool": "rocket-pool", "gearbox": "gearbox", "morphoVaults": "morpho-blue"}
LLAMA_CHAIN = {1: "Ethereum", 100: "Gnosis", 42161: "Arbitrum", 8453: "Base"}
# LST symbol -> the DeFiLlama pool that carries its intrinsic staking yield
LST_BASE = {"WSTETH": ("lido", "STETH"), "STETH": ("lido", "STETH"), "WEETH": ("ether.fi-stake", "WEETH"),
            "OSETH": ("stakewise-v3", "OSETH"), "ETHX": ("stader", "ETHX"), "RETH": ("rocket-pool", "RETH")}
LENDING_ON_LST = {"gearbox", "aave_v3", "spark", "compound_v3", "morphoVaults"}


def lst_yields(by_key: dict) -> dict:
    """Current intrinsic APY per LST from DeFiLlama, as a fraction."""
    out = {}
    for sym, key in LST_BASE.items():
        cands = [p for p in by_key.get(key, []) if fnum(p.get("apy")) > 0]
        if cands:
            out[sym] = fnum(max(cands, key=lambda x: fnum(x.get("tvlUsd")))["apy"]) / 100
    return out


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
    if protocol == "gearbox":
        return a.upper().replace("KPK", "").replace("MARKET", "").strip()      # "kpk wstETH" -> WSTETH
    if protocol == "spark" and "SDAI" in a.upper():
        return "SDAI"
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
    # kpk vault families: DeFiLlama lists one pool per family (the live version). Price only the
    # highest version among the permitted rows; older versions stay unpriced so they are never proposed.
    fam: dict[str, int] = {}
    def family(r):
        m = re.match(r"^(.*?)(?:\s+v(\d+))?$", (r.get("asset") or "").strip(), re.I)
        return (m.group(1).lower(), int(m.group(2) or 1)) if m else ((r.get("asset") or "").lower(), 1)
    for r in rows:
        if r["protocol"] == "morphoVaults" and (r.get("asset") or "").lower().startswith("kpk"):
            f, v = family(r); fam[f] = max(fam.get(f, 0), v)
    lst = lst_yields(by_key)
    matched = unmatched = 0
    for r in rows:
        if r["protocol"] == "morphoVaults" and (r.get("asset") or "").lower().startswith("kpk"):
            f, v = family(r)
            if v < fam.get(f, v):
                r.update(priced=False, apy_total=None, apy_30d=None, apy_source=f"superseded by v{fam[f]}")
                unmatched += 1
                continue
        proj = LLAMA_PROJECT.get(r["protocol"])
        sym = llama_symbol(r["protocol"], r.get("asset")) if proj else None
        if sym in ("SUSDS", "SDAI"):
            proj = "sky-lending"
        cands = [p for p in by_key.get((proj, sym), []) if fnum(p.get("apy")) > 0] if proj and sym else []
        if not cands:
            unmatched += 1
            if r.get("priced"):
                r["apy_source"] = f"{r.get('apy_source') or 'cache'} (stale)"
            continue
        p = max(cands, key=lambda x: fnum(x.get("tvlUsd")))
        # composite: market rate + the collateral LST's own staking yield (vaults.fyi's apyComposite does the same)
        intrinsic = lst.get(sym, 0.0) if r["protocol"] in LENDING_ON_LST and sym in LST_BASE else 0.0
        r.update(apy_total=fnum(p.get("apy")) / 100 + intrinsic, apy_base=(fnum(p.get("apyBase")) / 100 + intrinsic) if p.get("apyBase") is not None else None,
                 apy_reward=(fnum(p.get("apyReward")) / 100) if p.get("apyReward") is not None else None,
                 apy_30d=(fnum(p.get("apyMean30d")) / 100 + intrinsic) if p.get("apyMean30d") is not None else r.get("apy_30d"),
                 apy_1d=fnum(p.get("apy")) / 100 + intrinsic,   # DeFiLlama apy is the current (spot) rate
                 apy_intrinsic_lst=intrinsic or None,
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
    # 1. on-chain Roles gate: the Strategy API lists what ops-tools knows about, which can include venues whose PUR
    #    has not landed yet. Only the bot's parsed Roles file says what the Safe can actually call.
    targets = roles_targets(c)
    gated = roles_gate(rows, targets)
    if targets is None:
        print("  roles gate: live_permissions.json not found for this client; venues NOT verified against on-chain Roles")
    else:
        print(f"  roles gate: {len(targets)} targets in on-chain Roles; " +
              (f"EXCLUDED {len(gated)} permitted venue(s) not in Roles: " + ", ".join(f"{g['protocol']}/{g['asset']}" for g in gated) if gated else "all vault-type venues verified"))
    # 2. live APYs straight from vaults.fyi, independent of the Strategy API cache (works off-network)
    n_vf, vault_apys = vaults_fyi_refresh(rows, chain_id, a.period)
    if n_vf:
        print(f"  vaults.fyi live: {n_vf} venues repriced by vault address")
    # 3. DeFiLlama only for what is still unpriced (no vault address or no vaults.fyi entry)
    if stale_note or any(r["priced"] and r.get("apy_source") not in ("vaults.fyi live",) for r in rows):
        rest = [r for r in rows if r.get("apy_source") != "vaults.fyi live" and r.get("in_roles") is not False]
        m, u = live_apy_refresh(rest, chain_id) if rest else (0, 0)
        if stale_note:
            stale_note = (f"Strategy API unreachable: permissions from the office cache of {perms.get('vaultDataFetchedAt')}; "
                          f"APYs live from vaults.fyi for {n_vf} venues, DeFiLlama for {m}" + (f", {u} kept cached (stale)" if u else ""))
            print("  " + stale_note)
        elif m:
            print(f"  defillama live: {m} further venues repriced")
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
        vault_apys=vault_apys, roles_gate=dict(checked=targets is not None, n_targets=len(targets or []),
                                               excluded=[dict(protocol=g["protocol"], asset=g["asset"], vault=g["vault"]) for g in gated]),
        raw_permissions=perms, raw_best_strategy=best,
        all_permissions_protocols=sorted((perms.get("allPermissions") or {}).keys()),
        ops_tools_best_strategy=best,
        ops_tools_caveat="Optimizer respects only tvlCapPercent per venue. It ignores policy caps/floors and swap-only venues. Reference only.",
        vaults_fyi_benchmarks=bench))
    print(f"DONE -> {out / 'yields.json'}")


if __name__ == "__main__":
    main()
