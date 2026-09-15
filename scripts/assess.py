"""Turn holdings.json + yields.json into a policy check and a yield-opportunity list.

    python assess.py --client ens --out <dir> [--min-pickup-bps 50] [--min-move-usd 250000]
                     [--venue-tvl-cap-pct 10] [--exclude gearbox fluid ...]

Reads <out>/holdings.json and <out>/yields.json (from fetch_holdings.py / fetch_yields.py) and
writes <out>/assessment.json and <out>/assessment.md. The markdown is the data appendix Claude
reads before writing the recommendation in references/output-format.md shape. Nothing here is
the recommendation itself: it lists facts, breaches and candidate moves with the numbers.

Method
  Book        = Strategy API positions (priced, with APY) + idle wallet assets from Syncrone/Etherscan
                + in-flight items from Syncrone. Asset groups: USD / ETH / EURO / OTHER.
  Policy      = clients.json policy block (ENS: IPS 2026). Floor first, then effective stable target,
                then per-protocol share of NAV, then whitelists. Reported as in-bounds / near / breached.
  Performance = per asset group, blended APY; laggards = positions below the group's best permitted
                venue by >= min-pickup; candidates = permitted venues (deposit/stake/swap) with APY,
                sized to min(position, venue TVL x cap, protocol-cap headroom). Idle assets at 0%
                and any position in an excluded venue are always actions.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from common import asset_group_of, client, fnum, registry, write_json

NEAR = 0.9  # 90% of a cap counts as "near the line"


def load(out: Path, name: str) -> dict:
    p = out / name
    if not p.exists():
        sys.exit(f"ERROR: {p} missing; run fetch_holdings.py / fetch_yields.py first")
    return json.loads(p.read_text(encoding="utf-8"))


def build_book(h: dict, reg: dict) -> list[dict]:
    book = []
    for r in h["positions"]:
        if r["source"] == "strategy_api":
            book.append(dict(kind="position", protocol=r["protocol"], venue=r["position"], vault=r.get("vault"),
                             symbol=r["symbol"], asset_group=r["asset_group"], usd=r["usd"],
                             apy=fnum(r.get("apy_total")), apy_base=fnum(r.get("apy_base")),
                             venue_tvl_usd=fnum(r.get("venue_tvl_usd")), source="strategy_api"))
    for r in h.get("rewards") or []:
        if r["usd"] >= 1 and r["claimable"]:
            book.append(dict(kind="reward", protocol=r["source"], venue=f"claimable {r['symbol']} ({r.get('note') or r['source']})", symbol=r["symbol"],
                             asset_group="REWARDS", usd=r["usd"], apy=None, apy_base=None, balance=r["amount"], source="rewards",
                             claimable=True, claim_cmd=r.get("claim_cmd"), claim_only=r.get("claim_only", False), token_id=r.get("token_id")))
    has_strat = any(b["source"] == "strategy_api" for b in book)
    for r in h["positions"]:
        if r["source"] not in ("syncrone", "coingecko"):
            continue
        if r["kind"] == "idle" and not r.get("spam") and r["usd"] > reg["spam_dust_usd"]:
            book.append(dict(kind="idle", protocol="(wallet)", venue=f"idle in Safe [{r['symbol']}]", symbol=r["symbol"],
                             asset_group=r["asset_group"], usd=r["usd"], apy=0.0, apy_base=0.0,
                             balance=r["balance"], source="syncrone"))
        elif r["kind"] == "position" and r.get("in_flight") and r["usd"] > reg["spam_dust_usd"]:
            book.append(dict(kind="in_flight", protocol=r["protocol"].lower(), venue=f"{r['position']} (withdrawal queue)",
                             symbol=r["symbol"], asset_group=r["asset_group"], usd=r["usd"], apy=0.0, apy_base=0.0,
                             balance=r["balance"], source="syncrone"))
        elif r["kind"] == "position" and (not has_strat or not r.get("covered_by_strategy_api", True)) \
                and r["usd"] > reg["spam_dust_usd"]:
            # Strategy API / vaults.fyi does not price this protocol: keep the Syncrone row, APY unknown
            proto = reg["protocol_aliases"].get(r["protocol"].lower(), r["protocol"].lower())
            book.append(dict(kind="position", protocol=proto, venue=f"{r['position']} [{r['symbol']}]", symbol=r["symbol"],
                             asset_group=r["asset_group"], usd=r["usd"], apy=None, apy_base=None, source="syncrone",
                             untracked=True, realised_apr=r.get("realised_apr"), realised_days=r.get("realised_days")))
    return book


def policy_checks(book: list[dict], nav: float, pol: dict | None, reg: dict) -> list[dict]:
    if not pol:
        return []
    out = []
    by_grp = {}
    for b in book:
        by_grp[b["asset_group"]] = by_grp.get(b["asset_group"], 0.0) + b["usd"]
    stables = by_grp.get("USD", 0.0) + by_grp.get("EURO", 0.0)
    volatile = by_grp.get("ETH", 0.0)
    if pol.get("stable_floor_usd"):
        floor = pol["stable_floor_usd"]
        st = "breached" if stables < floor else ("near" if stables < floor * 1.05 else "in-bounds")
        out.append(dict(check="Stablecoin floor (unconditional, senior to the split)", status=st,
                        value=f"stables ${stables:,.0f} vs floor ${floor:,.0f} ({'short' if stables < floor else 'buffer'} ${abs(stables-floor):,.0f})",
                        effective_stable_target_pct=round(max(pol["target_stable_pct"], 100 * floor / nav), 1) if nav else None))
        eff = max(pol["target_stable_pct"], 100 * floor / nav) if nav else pol["target_stable_pct"]
    else:
        eff = pol.get("target_stable_pct")
    if eff is not None and nav:
        sp = 100 * stables / nav
        drift = sp - eff
        st = "in-bounds" if abs(drift) <= 3 else ("near" if abs(drift) <= 6 else "breached")
        out.append(dict(check="Target allocation (stables vs volatile, by market value)", status=st,
                        value=f"stables {sp:.1f}% / volatile {100*volatile/nav:.1f}% vs effective stable target {eff:.1f}% "
                              f"(nominal {pol['target_stable_pct']}%); drift {drift:+.1f} pts"))
    cap = pol.get("protocol_cap_pct_nav")
    if cap and nav:
        by_proto = {}
        for b in book:
            if b["kind"] in ("position", "in_flight"):
                by_proto[b["protocol"]] = by_proto.get(b["protocol"], 0.0) + b["usd"]
        for p, v in sorted(by_proto.items(), key=lambda kv: -kv[1]):
            share = 100 * v / nav
            st = "breached" if share > cap else ("near" if share > cap * NEAR else "in-bounds")
            out.append(dict(check=f"Single-protocol cap {cap}% of NAV: {p}", status=st,
                            value=f"{share:.1f}% (${v:,.0f}); headroom ${max(0.0, cap/100*nav - v):,.0f}"))
    nets = pol.get("networks")
    if nets:
        out.append(dict(check="Network whitelist", status="in-bounds", value=f"book read on {', '.join(nets)} only; other chains not pulled"))
    allowed = set(map(str.upper, (pol.get("allowed_stables") or []) + (pol.get("allowed_eth") or [])))
    if allowed:
        odd = sorted({b["symbol"] for b in book if b["kind"] == "idle" and b["symbol"] and b["symbol"].upper() not in allowed})
        out.append(dict(check="Asset whitelist (idle wallet tokens)", status="in-bounds" if not odd else "near",
                        value="all idle tokens on the allowed list" if not odd else f"idle tokens outside the list: {', '.join(odd)} (dust/airdrop unless sizeable)"))
    return out


def performance(book, permitted, nav, pol, args, reg):
    """Per asset group: blended APY, laggards, candidate venues, sized moves."""
    cap_pct = (pol or {}).get("protocol_cap_pct_nav")
    by_proto = {}
    for b in book:
        if b["kind"] in ("position", "in_flight"):
            by_proto[b["protocol"]] = by_proto.get(b["protocol"], 0.0) + b["usd"]
    excluded = {e.lower() for e in args.exclude}
    groups = {}
    STABLE_SYMS = {"USDC", "USDT", "USDS", "DAI", "GHO", "EURC", "PYUSD", "RLUSD"}
    IDLE_FLOOR_USD = 5_000     # smaller idle balances are noise, not actions
    for grp in sorted({b["asset_group"] for b in book} | {p["asset_group"] for p in permitted}):
        if grp in ("OTHER", "REWARDS"):
            continue            # governance / non-yield tokens are never rotated; rewards go through the sweep
        pos = [b for b in book if b["asset_group"] == grp and b["kind"] == "position" and b["usd"] > 1000]
        idle = [b for b in book if b["asset_group"] == grp and b["kind"] == "idle" and b["usd"] >= IDLE_FLOOR_USD]
        infl = [b for b in book if b["asset_group"] == grp and b["kind"] == "in_flight"]
        total = sum(b["usd"] for b in pos + idle + infl)
        if total < 1000:
            continue
        priced = [b for b in pos if b["apy"] is not None]
        blended = sum(b["usd"] * b["apy"] for b in priced) / sum(b["usd"] for b in priced) if priced else None
        venues = [p for p in permitted if p["asset_group"] == grp and p["priced"] and p["action"] in ("deposit", "stake", "swap")
                  and p["protocol"].lower() not in excluded]
        venues.sort(key=lambda p: -fnum(p["apy_total"]))
        best = venues[0] if venues else None
        # laggards: positions >= min pickup below the best permitted venue in the same group
        min_pick = args.min_pickup_bps / 10_000
        laggards, moves = [], []
        for b in sorted(pos, key=lambda b: fnum(b["apy"])):
            if b["apy"] is None or not best:
                continue
            gap = fnum(best["apy_total"]) - b["apy"]
            if b["protocol"].lower() in excluded:
                laggards.append(dict(**b, reason="excluded venue: exit is mandatory regardless of yield", gap_to_best=gap))
            elif gap >= min_pick and b["usd"] >= args.min_move_usd:
                laggards.append(dict(**b, reason=f"{gap*100:.2f} pts below best permitted venue {best['protocol']}/{best['asset']}", gap_to_best=gap))
        # size candidate moves: fill best venues subject to venue TVL cap and protocol cap headroom
        headroom = {}
        for v in venues:
            p = v["protocol"]
            proto_room = (cap_pct / 100 * nav - by_proto.get(p, 0.0)) if (cap_pct and nav) else float("inf")
            tvl_room = (args.venue_tvl_cap_pct / 100 * fnum(v["tvl_usd"])) if v["tvl_usd"] else float("inf")
            headroom[id(v)] = max(0.0, min(proto_room, tvl_room))
        sources = [dict(b) for b in idle] + [dict(l) for l in laggards]
        for src in sources:
            remaining = src["usd"]
            for v in venues:
                if remaining < args.min_move_usd and src["kind"] != "idle":
                    break
                if v["protocol"] == src["protocol"] and v.get("asset") == src.get("symbol"):
                    continue
                # token compatibility: same token, or stable-to-stable (swap), or the ETH family
                st, vt = (src.get("symbol") or "").upper(), (v.get("asset") or "").upper()
                same = st == vt or any(t in st and t in vt for t in STABLE_SYMS)
                ok_swap = (any(t in st for t in STABLE_SYMS) and any(t in vt for t in STABLE_SYMS)) or grp == "ETH"
                if not (same or ok_swap):
                    continue
                pick = fnum(v["apy_total"]) - fnum(src["apy"])
                if pick < min_pick and src["kind"] == "position":
                    break  # venues are sorted; nothing better left
                amt = min(remaining, headroom[id(v)])
                if amt < min(args.min_move_usd, remaining):
                    continue
                moves.append(dict(from_kind=src["kind"], from_protocol=src["protocol"], from_venue=src["venue"],
                                  from_apy=fnum(src["apy"]), to_protocol=v["protocol"], to_asset=v["asset"],
                                  to_action=v["action"], to_vault=v.get("vault"), to_apy=fnum(v["apy_total"]),
                                  to_apy_30d=v.get("apy_30d"), to_venue_tvl_usd=v["tvl_usd"], amount_usd=round(amt),
                                  pickup_pts=round(pick * 100, 2), pickup_usd_per_year=round(amt * pick)))
                headroom[id(v)] -= amt
                remaining -= amt
                if remaining <= 0:
                    break
        after_apy = None
        if priced:
            num = sum(b["usd"] * b["apy"] for b in priced) + sum(m["amount_usd"] * (m["to_apy"] - m["from_apy"]) for m in moves)
            den = sum(b["usd"] for b in priced) + sum(m["amount_usd"] for m in moves if m["from_kind"] == "idle")
            after_apy = num / den if den else None
        groups[grp] = dict(total_usd=round(total), positions=pos, idle=idle, in_flight=infl,
                           blended_apy=blended, blended_apy_after_moves=after_apy,
                           best_permitted=best, permitted_venues=venues[:10],
                           unpriced_permitted=sorted({f"{p['protocol']}/{p['asset']}" for p in permitted
                                                      if p["asset_group"] == grp and not p["priced"] and p["action"] != "swap"}),
                           laggards=laggards, candidate_moves=moves,
                           pickup_usd_per_year=round(sum(m["pickup_usd_per_year"] for m in moves)))
    return groups


def render_md(c, h, y, book, nav, checks, perf, args) -> str:
    L = []
    L.append(f"# {c['display_name']} — rebalancing data appendix")
    L.append(f"Generated {dt.datetime.now(dt.timezone.utc).isoformat(timespec='minutes')} · chain {h['chain_id']} · avatar Safe `{h['avatar_safe']}` · APY period {h['period']} · vault data {y.get('vault_data_fetched_at')}")
    L.append("")
    L.append("## NAV and reconciliation")
    n = h["nav"]
    L.append(f"- Syncrone NAV (this Safe, this chain) **${n['syncrone_nav_usd']:,.0f}** = positions ${n['syncrone_positions_usd']:,.0f} (incl. in-flight ${n['syncrone_in_flight_usd']:,.0f}) + idle ${n['syncrone_idle_usd']:,.0f}")
    L.append(f"- Bridge: Strategy API positions ${n['strategy_api_positions_usd']:,.0f} + untracked-by-optimizer ${n['untracked_by_strategy_api_usd']:,.0f} + in-flight ${n['syncrone_in_flight_usd']:,.0f} + idle ${n['syncrone_idle_usd']:,.0f} = **${n['bridge_usd']:,.0f}**")
    L.append(f"- On-chain idle ETH (Safe = Etherscan): {n['onchain_idle_eth']:,.4f} ETH (${n['onchain_idle_eth_usd']:,.0f} at ${n['eth_price_usd']:,.0f})")
    dd = n.get("syncrone_vs_bridge_diff")
    L.append(f"- Difference: **{'n/a' if dd is None else f'{dd:.2%}'}** (tolerance 1%)")
    toks = h["reconciliation"]["tokens"]
    L.append(f"- Safe vs Etherscan balances for {len(toks)} named tokens: {sum(1 for t in toks if t['safe_vs_etherscan'] is not None and t['safe_vs_etherscan'] <= 0.01)} agree, "
             f"{sum(1 for t in toks if t['safe_vs_etherscan'] is not None and t['safe_vs_etherscan'] > 0.01)} disagree")
    if h.get("other_wallets_in_syncrone_org"):
        L.append("- Other wallets/chains in the same Syncrone org, not assessed here: " + ", ".join(f"{k} ${v:,.0f}" for k, v in h["other_wallets_in_syncrone_org"].items()))
    for f in h["flags"]:
        L.append(f"- {'⚠️ ' if f.startswith(('NAV:', 'STOP')) else ''}{f}")
    L.append("")
    L.append("## Book used for the assessment")
    L.append(f"NAV used: **${nav:,.0f}** (Strategy API positions + untracked positions + idle + in-flight, this Safe and chain only).")
    L.append("")
    L.append("| Group | Protocol | Venue | Value USD | Share | APY |")
    L.append("|---|---|---|---:|---:|---:|")
    for b in sorted(book, key=lambda b: (b["asset_group"], -b["usd"])):
        if b["usd"] < 1000:
            continue
        src = b.get("apy_source")
        tag = "" if not src or src == "vaults.fyi" else f" ({src})"
        L.append(f"| {b['asset_group']} | {b['protocol']} | {b['venue']}{' ⏳' if b['kind']=='in_flight' else ''} | {b['usd']:,.0f} | {100*b['usd']/nav:.1f}% | {'n/a' if b['apy'] is None else f'{b['apy']*100:.2f}%'}{tag} |")
    L.append("")
    if checks:
        L.append(f"## Policy checks — {c['policy']['name']}")
        L.append("Authoritative text: load the `" + c["policy"].get("policy_skill", "") + "` skill before quoting.")
        L.append("")
        L.append("| Check | Status | Detail |")
        L.append("|---|---|---|")
        for k in checks:
            icon = {"breached": "🔴", "near": "🟠", "in-bounds": "🟢"}[k["status"]]
            L.append(f"| {k['check']} | {icon} {k['status']} | {k['value']}" + (f" · effective stable target {k['effective_stable_target_pct']}%" if k.get("effective_stable_target_pct") else "") + " |")
        L.append("")
    else:
        L.append("## Policy checks")
        L.append("No investment policy configured for this client in clients.json: pure yield optimisation within the Roles permissions.")
        L.append("")
    if rew_lines := [b for b in book if b["asset_group"] == "REWARDS"]:
        L.append("## Rewards")
        L.append("| Source | Token | Amount | Value USD | State |")
        L.append("|---|---|---:|---:|---|")
        for b in rew_lines:
            L.append(f"| {b['protocol']} | {b['symbol']} | {fnum(b.get('balance')):,.4f} | {b['usd']:,.0f} | {'claimable' if b['kind'] == 'reward' else 'held in Safe'} |")
        L.append("Sweep = claim, swap to USDC on CoW (stage 1), deposit into the best permitted USDC venue (stage 2).")
        L.append("")
    L.append("## Yield assessment (within permissions)")
    L.append(f"Thresholds: pickup ≥ {args.min_pickup_bps} bps, move ≥ ${args.min_move_usd:,.0f}, venue TVL cap {args.venue_tvl_cap_pct}% per venue"
             + (f", excluded venues: {', '.join(args.exclude)}" if args.exclude else "") + ".")
    for grp, g in perf.items():
        L.append("")
        L.append(f"### {grp} — ${g['total_usd']:,.0f}" + (f", blended APY {g['blended_apy']*100:.2f}%" if g["blended_apy"] is not None else "")
                 + (f" → {g['blended_apy_after_moves']*100:.2f}% after candidate moves (+${g['pickup_usd_per_year']:,.0f}/yr)" if g["blended_apy_after_moves"] is not None and g["candidate_moves"] else ""))
        if g["best_permitted"]:
            L.append("Best permitted venues (priced by vaults.fyi): " + ", ".join(
                f"{v['protocol']}/{v['asset']} {fnum(v['apy_total'])*100:.2f}%" + (f" (30d {fnum(v['apy_30d'])*100:.2f}%)" if v.get("apy_30d") is not None else "") + (f", TVL ${fnum(v['tvl_usd'])/1e6:,.0f}M" if v["tvl_usd"] else "")
                for v in g["permitted_venues"][:6]))
        if g["unpriced_permitted"]:
            L.append("Permitted but no APY feed (check manually before proposing): " + ", ".join(g["unpriced_permitted"]))
        if g["idle"]:
            L.append("Idle at 0%: " + ", ".join(f"{b['balance']:,.2f} {b['symbol']} (${b['usd']:,.0f})" for b in g["idle"]))
        if g["in_flight"]:
            L.append("In-flight (withdrawal queue, not deployable yet): " + ", ".join(f"{b['balance']:,.2f} {b['symbol']} in {b['protocol']} (${b['usd']:,.0f})" for b in g["in_flight"]))
        if g["laggards"]:
            L.append("Laggards: " + "; ".join(f"{l['protocol']} {l['venue']} ${l['usd']:,.0f} at {l['apy']*100:.2f}% ({l['reason']})" for l in g["laggards"]))
        if g["candidate_moves"]:
            L.append("")
            L.append("| From | To | Amount USD | APY from → to | Pickup /yr |")
            L.append("|---|---|---:|---|---:|")
            for m in g["candidate_moves"]:
                L.append(f"| {m['from_protocol']} {m['from_venue']} | {m['to_protocol']} {m['to_asset']} ({m['to_action']}) | {m['amount_usd']:,.0f} | {m['from_apy']*100:.2f}% → {m['to_apy']*100:.2f}% | {m['pickup_usd_per_year']:,.0f} |")
        elif not g["idle"]:
            L.append("No move clears the thresholds; note laggards rather than forcing a trade.")
    ob = (y.get("ops_tools_best_strategy") or {})
    if ob.get("recommendations"):
        L.append("")
        L.append("## ops-tools optimizer output (reference only)")
        L.append(y.get("ops_tools_caveat", ""))
        for r in ob["recommendations"]:
            L.append(f"- {r['assetGroup']}: {fnum(r['currentWeightedAPY'])*100:.2f}% → {fnum(r['recommendedWeightedAPY'])*100:.2f}% by moving ${fnum(r['changedValue']):,.0f}; "
                     + ", ".join(f"{a['protocol']}/{a['vaultName']} → ${fnum(a['recommendedValue']):,.0f}" for a in r["allocations"] if fnum(a["recommendedValue"]) > 0))
    L.append("")
    L.append("## Caveats to carry into the recommendation")
    L.append("- APYs are vaults.fyi figures via the Strategy API for the selected period; reward-bearing wrappers (weETH, wstETH, osETH, ETHx) earn through their exchange rate even when 'idle'.")
    L.append("- Syncrone `apy_pct` is month-to-date return, not an annualised yield; it is not used here.")
    L.append("- Permitted venues without an APY feed may be better than the priced ones; they are listed, not ranked.")
    L.append("- Exit liquidity is not checked here: confirm pool utilisation before sizing a withdrawal from a lending market.")
    L.append("- Sizing respects the venue TVL cap and the policy protocol cap; it does not model gas, slippage or swap routes.")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--min-pickup-bps", type=float, default=50)
    ap.add_argument("--min-move-usd", type=float, default=250_000)
    ap.add_argument("--venue-tvl-cap-pct", type=float, default=10)
    ap.add_argument("--exclude", nargs="*", default=[], help="protocol keys to treat as excluded venues (hack, blacklist)")
    args = ap.parse_args()
    reg = registry()
    c = client(args.client)
    out = Path(args.out)
    h = load(out, "holdings.json")
    y = load(out, "yields.json")
    book = build_book(h, reg)
    # Off-network: Syncrone rows are the whole book; match APY from the permitted venues (vaults.fyi via
    # the last snapshot) by protocol + asset group + token, before the DeFiLlama fallback.
    if y.get("stale_note"):
        perm = [p for p in y.get("permitted", []) if p.get("priced")]
        for b in book:
            if b["kind"] != "position" or b["apy"] is not None:
                continue
            norm = lambda s: "".join(ch for ch in (s or "").lower() if ch.isalnum())
            sym, ven = norm(b.get("symbol")), norm(b.get("venue"))
            cands = [p for p in perm if p["protocol"] == b["protocol"] and p["asset_group"] == b["asset_group"]]
            def score(p):
                a = norm(p.get("asset"))
                if not a:
                    return 0
                if a == sym or a in ven:
                    return 3
                if a in sym or sym in a:
                    return 2
                # "kpk usdc prime v2" vs "KPK_USDC_Prime": all words of the asset minus a version tag
                words = [w for w in (p.get("asset") or "").lower().split() if not (w.startswith("v") and w[1:].isdigit())]
                return 1 if words and all(norm(w) in sym for w in words) else 0
            ranked = sorted(cands, key=lambda p: -score(p))
            hit = ranked[0] if ranked and score(ranked[0]) > 0 else None
            if hit and score(hit) == 1:
                # prefer the newest version when several share the same words
                same = [p for p in cands if score(p) == 1]
                hit = sorted(same, key=lambda p: (p.get("asset") or ""))[-1]
            if hit is None and len({p.get("asset") for p in cands}) == 1:
                hit = cands[0]
            if hit:
                b["apy"], b["venue_tvl_usd"] = hit.get("apy_total"), hit.get("tvl_usd")
                b["apy_source"] = hit.get("apy_source") or f"vaults.fyi (stale, {y.get('vault_data_fetched_at')})"
                b["untracked"] = False
                b["vault"] = hit.get("vault")
    # fallback APY (DeFiLlama) for untracked sleeves: most specific key wins
    fb = y.get("fallback_apy") or {}
    for b in book:
        if b["kind"] == "position" and b["apy"] is None and fb:
            for k in (f"{b['protocol']}/{b['asset_group']}/{b.get('symbol')}", f"{b['protocol']}/{b['asset_group']}"):
                if k in fb and fb[k]["apy"] > 0:   # DeFiLlama reports 0 when it cannot compute; treat as unknown
                    b["apy"], b["apy_source"], b["apy_pool"] = fb[k]["apy"], "defillama", fb[k]["pool"]
                    b["venue_tvl_usd"] = fb[k]["tvl_usd"]
                    break
    for b in book:
        if b["kind"] == "position" and b["apy"] is None and 0.0005 <= (b.get("realised_apr") or 0) <= 0.20 and b["usd"] >= 10_000:
            # third tier: Syncrone realised yield this month, annualised. Noisy early in the month; labelled.
            # Zero means Syncrone has booked no yield yet (e.g. stkAAVE rewards), so it stays unknown.
            b["apy"], b["apy_source"] = b["realised_apr"], f"syncrone realised, {b.get('realised_days')}d annualised"
    for b in book:
        if b["kind"] == "position" and b["apy"] is not None and "apy_source" not in b:
            b["apy_source"] = "vaults.fyi" if b["source"] == "strategy_api" else "unknown"
    nav = sum(b["usd"] for b in book)
    checks = policy_checks(book, nav, c.get("policy"), reg)
    perf = performance(book, y["permitted"], nav, c.get("policy"), args, reg)
    # rewards sweep: claim everything claimable + held reward tokens, swap to USDC via CoW, deposit in the best USD venue
    rew_all = [b for b in book if b["asset_group"] == "REWARDS"]
    rew = [b for b in rew_all if not b.get("claim_only")]
    claim_only = [dict(symbol=b["symbol"], amount=b.get("balance"), usd=round(b["usd"]), source=b["protocol"], claim_cmd=b.get("claim_cmd"),
                       token_id=b.get("token_id"), venue=b["venue"]) for b in rew_all if b.get("claim_only")]
    usd_venues = sorted([p for p in y.get("permitted", []) if p["asset_group"] == "USD" and p["priced"] and p["action"] == "deposit"
                         and "USDC" in (p.get("asset") or "").upper()], key=lambda p: -fnum(p["apy_total"]))
    sweep = dict(total_usd=round(sum(b["usd"] for b in rew)), min_usd=reg.get("rewards", {}).get("min_sweep_usd", 100),
                 items=[dict(symbol=b["symbol"], amount=b.get("balance"), usd=round(b["usd"]), source=b["protocol"], claimable=b["kind"] == "reward",
                             claim_cmd=b.get("claim_cmd")) for b in rew],
                 claim_only=claim_only,
                 best_usd_venue=(dict(protocol=usd_venues[0]["protocol"], asset=usd_venues[0]["asset"], vault=usd_venues[0].get("vault"),
                                      apy=usd_venues[0]["apy_total"]) if usd_venues else None))
    write_json(out / "assessment.json", dict(client=args.client, nav_usd=round(nav), book=book, policy_checks=checks,
                                              performance=perf, rewards_sweep=sweep, thresholds=vars(args)))
    md = render_md(c, h, y, book, nav, checks, perf, args)
    (out / "assessment.md").write_text(md, encoding="utf-8")
    print(f"[{args.client}] NAV ${nav:,.0f}; policy: " + (", ".join(f"{k['status']}" for k in checks) or "none") +
          "; candidate moves: " + str(sum(len(g["candidate_moves"]) for g in perf.values())) +
          f"; pickup ${sum(g['pickup_usd_per_year'] for g in perf.values()):,.0f}/yr")
    print(f"DONE -> {out / 'assessment.md'}")


if __name__ == "__main__":
    main()
