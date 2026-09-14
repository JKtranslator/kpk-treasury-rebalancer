"""Pull current holdings for a kpk client treasury from three independent sources and reconcile.

    python fetch_holdings.py --client ens --out <dir> [--period 7day] [--chain 1]

Sources (all read-only):
  1. Syncrone v2 performance API  -> priced positions + wallet holdings (the accounting view).
     Window = 1st of the current month .. 1st of next month; we read the `final` cut.
  2. KPK Strategy API (ops-tools-backend.kpk) /clients/current-strategy -> vault positions with
     APY and lpTokenBalance (vaults.fyi-backed operational view).
  3. Safe Transaction Service balances for the avatar Safe -> raw on-chain token balances.
  4. Etherscan V2 -> ETH balance + ERC-20 balanceOf for every token seen above (independent RPC view).

Writes <out>/holdings.json with:
  positions[]        unified rows (source, protocol, position, token, balance, usd, apy where known)
  nav                per-source totals and the NAV bridge
  reconciliation     token-level (Safe vs Etherscan vs Syncrone vs Strategy API) and NAV-level checks
  flags[]            anything that disagrees by more than the tolerance (default 1%)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

from common import (asset_group_of, client, fnum, http_json, key, load_env, registry,
                    write_json)

TOL = 0.01
DUST_USD = 50.0
ZERO = "0x0000000000000000000000000000000000000000"


def month_window(today: dt.date) -> tuple[str, str]:
    start = today.replace(day=1)
    nxt = (start + dt.timedelta(days=32)).replace(day=1)
    return start.isoformat(), nxt.isoformat()


# ---------------------------------------------------------------- Syncrone
def fetch_syncrone(c: dict, reg: dict) -> dict | None:
    if not c.get("syncrone_org"):
        return None
    k = key("SYNCRONE_API_KEY", required=True)
    frm, to = month_window(dt.date.today())
    url = f"{reg['syncrone_api_base']}/{c['syncrone_org']}/performance?from={frm}&to={to}"
    d = http_json(url, headers={"x-api-key": k}, timeout=180)
    org = (d.get("organizations") or [d])[0]
    org["_window"] = {"from": frm, "to": to}
    return org


def syncrone_rows(org: dict, reg: dict) -> list[dict]:
    rows = []
    for w in org.get("wallets") or []:
        wa = (w.get("wallet_address") or "").lower()
        receipts = reg.get("receipt_tokens", {})
        for h in w.get("holdings") or []:
            f = h.get("final") or {}
            usd = fnum(f.get("balance_usd"))
            tok = h.get("token") or {}
            addr = (tok.get("address") or "").lower()
            rc = receipts.get(addr)
            if rc and usd >= DUST_USD:
                # Syncrone books this receipt token as a wallet holding; it is a position
                rows.append(dict(source="syncrone", kind="position", wallet=wa, protocol=rc["protocol"],
                                 position=rc["position"], position_type="receipt_token", position_id=f"receipt-{addr}",
                                 asset_id=None, in_flight=False, chain=h.get("chain_id"), token=addr,
                                 symbol=tok.get("symbol") or rc.get("symbol"), balance=fnum(f.get("balance")),
                                 price=fnum(f.get("price_usd")), usd=usd, mtd_roi_pct=None, mtd_apy_pct=None,
                                 asset_group=rc.get("asset_group") or asset_group_of(tok.get("symbol"), reg), spam=False,
                                 reclassified_from_idle=True))
                continue
            rows.append(dict(source="syncrone", kind="idle", wallet=wa, protocol="(wallet)",
                             position="idle holding", chain=h.get("chain_id"),
                             token=addr, symbol=tok.get("symbol"),
                             balance=fnum(f.get("balance")), price=fnum(f.get("price_usd")), usd=usd,
                             asset_group=asset_group_of(tok.get("symbol"), reg),
                             spam=usd < DUST_USD))
        for p in w.get("protocols") or []:
            pn = p.get("protocol_name") or ""
            for pos in p.get("positions") or []:
                for a in pos.get("assets") or []:
                    f = a.get("final") or {}
                    tok = a.get("token") or {}
                    m = a.get("metrics") or {}
                    aid = (a.get("id") or "").lower()
                    in_flight = any(t in aid for t in ("withdraw_process", "withdraw_queue", "unstake", "pending", "claimable"))
                    # realised yield this month, annualised: third-tier APY source for sleeves nobody prices
                    i0 = a.get("initial") or {}
                    days = max(1.0, (dt.date.today() - dt.date.today().replace(day=1)).days + dt.datetime.now(dt.timezone.utc).hour / 24)
                    base_val = fnum(i0.get("balance_usd")) or fnum(f.get("balance_usd"))
                    realised = (fnum(m.get("yield_pnl_usd")) / base_val) * (365.0 / days) if base_val > 1000 else None
                    rows.append(dict(source="syncrone", kind="position", wallet=wa, protocol=pn,
                                     position=pos.get("position_name") or "",
                                     position_type=pos.get("position_type"),
                                     position_id=pos.get("id"), asset_id=a.get("id"),
                                     in_flight=in_flight, chain=pos.get("chain_id"),
                                     token=(tok.get("address") or "").lower(), symbol=tok.get("symbol"),
                                     balance=fnum(f.get("balance")), price=fnum(f.get("price_usd")),
                                     usd=fnum(f.get("balance_usd")),
                                     mtd_roi_pct=m.get("roi_pct"), mtd_apy_pct=m.get("apy_pct"),
                                     realised_apr=realised, realised_days=round(days, 1),
                                     yield_pnl_usd=fnum(m.get("yield_pnl_usd")),
                                     asset_group=asset_group_of(tok.get("symbol"), reg),
                                     spam=False))
    return rows


# ---------------------------------------------------------------- Strategy API
def fetch_strategy_current(c: dict, reg: dict, chain_id: int, period: str) -> dict | None:
    if not c.get("strategy_api_name"):
        return None
    chain = reg["strategy_api_chain_names"][str(chain_id)]
    return http_json(f"{reg['strategy_api_base']}/clients/current-strategy",
                     data={"clientName": c["strategy_api_name"], "period": period, "chain": chain},
                     timeout=240)


def strategy_rows(d: dict, chain_id: int) -> list[dict]:
    rows = []
    for g in (d or {}).get("strategy") or []:
        for p in g.get("positions") or []:
            rows.append(dict(source="strategy_api", kind="position", protocol=p.get("protocol"),
                             position=p.get("vaultName"), vault=(p.get("vault") or "").lower(),
                             chain=chain_id, symbol=p.get("asset"), asset_group=g.get("assetGroup"),
                             lp_balance_raw=p.get("lpTokenBalance"),
                             usd=fnum(p.get("positionValueUsd")),
                             apy_total=fnum((p.get("apy") or {}).get("total")),
                             apy_base=fnum((p.get("apy") or {}).get("base")),
                             apy_reward=fnum((p.get("apy") or {}).get("reward")),
                             venue_tvl_usd=fnum((p.get("tvl") or {}).get("usd"))))
    return rows


# ---------------------------------------------------------------- Safe
def fetch_safe_balances(reg: dict, chain_id: int, safe: str) -> list[dict]:
    svc = reg["safe_tx_service"][str(chain_id)]
    k = key("SAFE_API_KEY")
    url = f"{svc['gateway'] if k else svc['legacy']}/safes/{safe}/balances/?trusted=false&exclude_spam=true"
    hdr = {"Authorization": f"Bearer {k}"} if k else {}
    d = http_json(url, headers=hdr)
    out = []
    for b in d:
        t = b.get("token") or {}
        dec = int(t.get("decimals") or 18)
        out.append(dict(source="safe", token=(b.get("tokenAddress") or ZERO).lower(),
                        symbol=t.get("symbol") or "ETH", decimals=dec,
                        balance=int(b.get("balance") or 0) / 10 ** dec, raw=b.get("balance")))
    return out


# ---------------------------------------------------------------- Etherscan
BLOCKSCOUT = {100: "https://gnosis.blockscout.com/api", 42161: "https://arbitrum.blockscout.com/api",
              8453: "https://base.blockscout.com/api", 10: "https://optimism.blockscout.com/api"}


PUBLIC_RPC = {1: "https://ethereum-rpc.publicnode.com", 100: "https://rpc.gnosischain.com",
              42161: "https://arb1.arbitrum.io/rpc", 8453: "https://mainnet.base.org", 10: "https://mainnet.optimism.io"}


class ExplorerClient:
    """Etherscan V2 first; if the free tier refuses the chain, Blockscout's Etherscan-compatible
    API; if that is blocked, a public JSON-RPC node (eth_getBalance / balanceOf). All three are
    independent of Safe's indexer, which is the point of this read."""

    def __init__(self, chain_id: int):
        self.chain_id = chain_id
        self.key = key("ETHERSCAN_API_KEY", required=True)
        self.base = f"https://api.etherscan.io/v2/api?chainid={chain_id}"
        self.name = "etherscan"

    def _rpc(self, method: str, params: list) -> str:
        d = http_json(PUBLIC_RPC[self.chain_id], data={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if "error" in d:
            raise RuntimeError(f"rpc: {d['error']}")
        return str(int(d["result"], 16))

    def call(self, params: dict) -> str:
        if self.name == "rpc":
            if params["action"] == "balance":
                return self._rpc("eth_getBalance", [params["address"], "latest"])
            data = "0x70a08231" + params["address"].lower().replace("0x", "").rjust(64, "0")
            return self._rpc("eth_call", [{"to": params["contractaddress"], "data": data}, "latest"])
        q = "&".join(f"{a}={b}" for a, b in params.items())
        url = f"{self.base}&{q}" + (f"&apikey={self.key}" if self.name == "etherscan" else "")
        try:
            d = http_json(url)
        except RuntimeError as e:
            if self.name == "blockscout" and self.chain_id in PUBLIC_RPC:
                print(f"  blockscout refused ({str(e)[:60]}...); using public RPC {PUBLIC_RPC[self.chain_id]}")
                self.name = "rpc"
                return self.call(params)
            raise
        if str(d.get("status")) != "1":
            msg = f"{d.get('message')} {d.get('result')}"
            if "rate limit" in msg.lower():
                self._rl = getattr(self, "_rl", 0) + 1
                if self._rl <= 6:
                    time.sleep(1.2 * self._rl)
                    return self.call(params)
            if self.name == "etherscan" and "not supported for this chain" in msg and self.chain_id in BLOCKSCOUT:
                self.base, self.name = BLOCKSCOUT[self.chain_id] + "?", "blockscout"
                print(f"  etherscan free tier does not cover chain {self.chain_id}; using Blockscout")
                return self.call(params)
            raise RuntimeError(f"{self.name}: {msg}")
        return d["result"]


def fetch_etherscan(chain_id: int, safe: str, tokens: dict[str, int]) -> list[dict]:
    x = ExplorerClient(chain_id)
    out = [dict(source="etherscan", token=ZERO, symbol="ETH", decimals=18,
                balance=int(x.call(dict(module="account", action="balance", address=safe, tag="latest"))) / 1e18)]
    for addr, dec in tokens.items():
        if addr == ZERO:
            continue
        time.sleep(0.25 if x.name == "etherscan" else 0.1)  # etherscan free tier: 5 req/s
        try:
            raw = x.call(dict(module="account", action="tokenbalance", contractaddress=addr, address=safe, tag="latest"))
            out.append(dict(source="etherscan", explorer=x.name, token=addr, decimals=dec, balance=int(raw) / 10 ** dec))
        except Exception as e:  # keep going; report the gap
            out.append(dict(source="etherscan", explorer=x.name, token=addr, decimals=dec, balance=None, error=str(e)[:120]))
    return out


COINGECKO_PLATFORM = {1: "ethereum", 100: "xdai", 42161: "arbitrum-one", 8453: "base", 10: "optimistic-ethereum"}


def price_safe_balances(chain_id: int, safe_rows: list[dict], reg: dict) -> list[dict]:
    """Clients without Syncrone/Strategy API: price the Safe's tokens with CoinGecko (keyless) so
    idle balances at least have a USD figure. Unpriced tokens are kept with usd=0 and flagged."""
    plat = COINGECKO_PLATFORM.get(chain_id)
    rows = []
    if not plat:
        return rows
    spam_marks = ("$", "http", ".com", ".io", ".xyz", "claim", "gift", "visit", "reward", "airdrop", "bonus", "#", "!")
    candidates = [r for r in safe_rows if r["token"] != ZERO and r["balance"] > 0
                  and not any(m in (r["symbol"] or "").lower() for m in spam_marks)]
    prices = {}
    cg_key = key("COINGECKO_API_KEY")
    hdr = {"x-cg-demo-api-key": cg_key} if cg_key else {}
    batch = 50 if cg_key else 1          # free tier: one contract address per request, ~30 req/min
    for i in range(0, len(candidates), batch):
        chunk = ",".join(r["token"] for r in candidates[i:i + batch])
        try:
            prices.update(http_json(f"https://api.coingecko.com/api/v3/simple/token_price/{plat}?contract_addresses={chunk}&vs_currencies=usd",
                                    headers=hdr, timeout=60))
        except Exception as e:
            print("  coingecko:", str(e)[:120])
        time.sleep(0.5 if cg_key else 3.0)   # free tier is ~30 req/min; a COINGECKO_API_KEY (demo) lifts this
    try:
        eth = http_json("https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd")["ethereum"]["usd"]
    except Exception:
        eth = 0.0
    for r in safe_rows:
        px = eth if r["token"] == ZERO else fnum((prices.get(r["token"]) or {}).get("usd"))
        usd = r["balance"] * px
        rows.append(dict(source="coingecko", kind="idle", wallet="", protocol="(wallet)", position="idle holding (Safe, CoinGecko-priced)",
                         chain=chain_id, token=r["token"], symbol=r["symbol"], balance=r["balance"], price=px, usd=usd,
                         asset_group=asset_group_of(r["symbol"], reg), spam=(px == 0 or usd < DUST_USD), priced=px > 0))
    return rows


# ---------------------------------------------------------------- reconcile
def pct_diff(a: float, b: float) -> float | None:
    if a is None or b is None:
        return None
    base = max(abs(a), abs(b))
    return 0.0 if base == 0 else abs(a - b) / base


def scope_syncrone(sync_rows: list[dict], safe: str | None, chain_id: int) -> tuple[list[dict], list[dict]]:
    """Split Syncrone rows into the assessed wallet+chain and everything else in the org."""
    sa = (safe or "").lower()
    in_scope, other = [], []
    for r in sync_rows:
        ch = r.get("chain")
        same_chain = ch is None or int(ch) == int(chain_id) if str(ch).isdigit() or ch is None else str(ch).lower() in (str(chain_id), "ethereum" if chain_id == 1 else "")
        if (not sa or r["wallet"] == sa) and same_chain:
            in_scope.append(r)
        else:
            other.append(r)
    return in_scope, other


def reconcile(sync_rows, strat_rows, safe_rows, eth_rows, reg, c):
    """Token-level: only wallet-held tokens we can name (idle holdings per Syncrone, Strategy API
    vault tokens, native ETH) are compared Safe vs Etherscan vs the accounting sources; spam
    tokens and positions Syncrone books in underlying units are excluded. Protocol-level: USD by
    protocol, Syncrone vs Strategy API. Protocols the Strategy API does not cover (NXM staking,
    Uniswap LPs, Balancer pools, Gnosis positions) are 'untracked' and enter the book with no APY.
    NAV: Syncrone (this wallet, this chain) vs Strategy API + untracked + idle."""
    flags, token_checks = [], []
    by_addr_safe = {r["token"]: r for r in safe_rows}
    by_addr_eth = {r["token"]: r for r in eth_rows}
    sync_by_tok: dict[str, float] = {}
    in_flight_rows = [r for r in sync_rows if r["kind"] == "position" and r.get("in_flight")]
    for r in sync_rows:
        if r["kind"] == "idle" and not r["spam"]:
            sync_by_tok[r["token"]] = sync_by_tok.get(r["token"], 0.0) + r["balance"]
    strat_by_vault: dict[str, float] = {}
    for r in strat_rows:
        dec = by_addr_safe.get(r["vault"], {}).get("decimals", 18)
        if r.get("lp_balance_raw") is not None:
            strat_by_vault[r["vault"]] = strat_by_vault.get(r["vault"], 0.0) + int(r["lp_balance_raw"]) / 10 ** dec

    # receipt tokens Syncrone names on positions and the Safe actually holds: Safe vs Etherscan only
    receipt = {r["token"] for r in sync_rows if r["kind"] == "position" and r["token"] != ZERO
               and fnum(by_addr_safe.get(r["token"], {}).get("balance")) > 1e-6}
    universe = set(sync_by_tok) | set(strat_by_vault) | receipt | {ZERO}
    for tok in sorted(universe):
        s = by_addr_safe.get(tok, {}).get("balance")
        e = by_addr_eth.get(tok, {}).get("balance")
        y = sync_by_tok.get(tok)
        v = strat_by_vault.get(tok)
        sym = by_addr_safe.get(tok, {}).get("symbol") or next((r["symbol"] for r in sync_rows if r["token"] == tok), tok[:10])
        if max(fnum(s), fnum(e), fnum(y), fnum(v)) == 0:
            continue
        row = dict(token=tok, symbol=sym, safe=s, etherscan=e, syncrone=y, strategy_api=v,
                   safe_vs_etherscan=pct_diff(s, e), safe_vs_syncrone=pct_diff(s, y),
                   safe_vs_strategy=pct_diff(s, v))
        token_checks.append(row)
        if row["safe_vs_etherscan"] is not None and row["safe_vs_etherscan"] > TOL:
            flags.append(f"{sym}: Safe {s:,.4f} vs Etherscan {e:,.4f} differ by {row['safe_vs_etherscan']:.2%} (two on-chain reads disagree; retry before trusting either)")
        px = next((r["price"] for r in sync_rows if r["token"] == tok and r.get("price")), 0.0)
        material = max(fnum(s), fnum(e), fnum(y)) * px >= 1000 if px else True
        if row["safe_vs_syncrone"] is not None and row["safe_vs_syncrone"] > TOL and y and s and material:
            flags.append(f"{sym}: Safe {s:,.4f} vs Syncrone {y:,.4f} differ by {row['safe_vs_syncrone']:.2%} (Syncrone books this position in a different unit, or it lags on-chain)")
        if row["safe_vs_strategy"] is not None and row["safe_vs_strategy"] > TOL and v:
            # The Strategy API labels some positions by the vault's underlying (eETH for weETH,
            # stETH for a wstETH balance), so the unit can differ. The USD sleeve check below is
            # the enforcement; this stays a note.
            row["note"] = (f"Strategy API lpTokenBalance {v:,.4f} vs Safe {fnum(s):,.4f}: vault token is not the receipt token "
                           f"the Safe holds (wrapper/underlying mismatch); compared at USD level only")

    # protocol-level USD: Syncrone vs Strategy API, keyed on (protocol, asset group) because the
    # Strategy API covers a protocol partially (Aave EURC but not Aave USDC, Lido stETH but not
    # an Aave-Lido WETH market). A Syncrone sleeve with no Strategy API counterpart is untracked.
    aliases = reg["protocol_aliases"]

    def pkey(proto: str, grp: str) -> str:
        return f"{aliases.get(proto.lower(), proto.lower())}/{grp}"

    # receipt tokens reclassified from idle: covered only if the Strategy API lists that exact
    # vault token (ciUSDCv3 is not cUSDCv3); otherwise they are their own untracked sleeve
    strat_vaults = {r["vault"] for r in strat_rows}
    receipt_untracked: dict[str, float] = {}
    sync_usd: dict[str, float] = {}
    for r in sync_rows:
        if r["kind"] != "position":
            continue
        if r.get("reclassified_from_idle") and r["token"] not in strat_vaults:
            k = f"{r['protocol']}/{r['asset_group']}/{r['symbol']}"
            receipt_untracked[k] = receipt_untracked.get(k, 0.0) + r["usd"]
            r["covered_by_strategy_api"] = False
            continue
        k = pkey(r["protocol"], r["asset_group"])
        sync_usd[k] = sync_usd.get(k, 0.0) + r["usd"]
    strat_usd: dict[str, float] = {}
    for r in strat_rows:
        k = f"{r['protocol']}/{r['asset_group']}"
        strat_usd[k] = strat_usd.get(k, 0.0) + r["usd"]
    in_flight_usd: dict[str, float] = {}
    for r in in_flight_rows:
        k = pkey(r["protocol"], r["asset_group"])
        in_flight_usd[k] = in_flight_usd.get(k, 0.0) + r["usd"]
    proto_checks, untracked = [], {}
    covered = set(strat_usd)
    for k in sorted(set(sync_usd) | set(strat_usd)):
        a, b = sync_usd.get(k, 0.0), strat_usd.get(k, 0.0)
        infl = in_flight_usd.get(k, 0.0)
        if k not in covered:
            if a - infl > DUST_USD:
                untracked[k] = a - infl
            proto_checks.append(dict(sleeve=k, syncrone_usd=round(a), syncrone_in_flight_usd=round(infl),
                                     strategy_api_usd=0, status="untracked_by_strategy_api"))
            continue
        d = pct_diff(a - infl, b)
        proto_checks.append(dict(sleeve=k, syncrone_usd=round(a), syncrone_in_flight_usd=round(infl),
                                 strategy_api_usd=round(b), diff_ex_in_flight=d, status="compared"))
        if d is not None and d > 0.03 and max(a, b) > 10_000:
            flags.append(f"{k}: Syncrone ${a:,.0f} (of which in-flight ${infl:,.0f}) vs Strategy API ${b:,.0f} differ by {d:.1%} after bridging in-flight items")
    for k, v in receipt_untracked.items():
        untracked[k] = v
        proto_checks.append(dict(sleeve=k, syncrone_usd=round(v), syncrone_in_flight_usd=0, strategy_api_usd=0,
                                 status="untracked_by_strategy_api (receipt token)"))
    # mark Syncrone rows the book should keep because the Strategy API does not price them
    for r in sync_rows:
        if r["kind"] == "position" and "covered_by_strategy_api" not in r:
            r["covered_by_strategy_api"] = pkey(r["protocol"], r["asset_group"]) in covered

    # NAV bridge
    sync_pos = sum(r["usd"] for r in sync_rows if r["kind"] == "position")
    sync_in_flight = sum(r["usd"] for r in in_flight_rows)
    sync_idle = sum(r["usd"] for r in sync_rows if r["kind"] == "idle" and not r["spam"])
    strat_total = sum(r["usd"] for r in strat_rows)
    untracked_total = sum(untracked.values())
    eth_px = next((r["price"] for r in sync_rows if r["token"] == ZERO and r["price"]), 0.0)
    eth_onchain = fnum(by_addr_eth.get(ZERO, {}).get("balance") if by_addr_eth else by_addr_safe.get(ZERO, {}).get("balance"))
    in_flight_items = [dict(protocol=r["protocol"], asset=r["symbol"], balance=r["balance"], usd=round(r["usd"]),
                            asset_id=r.get("asset_id")) for r in in_flight_rows if r["usd"] > DUST_USD]
    bridge = strat_total + untracked_total + sync_in_flight + sync_idle
    nav = dict(syncrone_positions_usd=round(sync_pos), syncrone_in_flight_usd=round(sync_in_flight),
               syncrone_idle_usd=round(sync_idle), syncrone_nav_usd=round(sync_pos + sync_idle),
               strategy_api_positions_usd=round(strat_total),
               untracked_by_strategy_api_usd=round(untracked_total), untracked_by_strategy_api=untracked,
               onchain_idle_eth=eth_onchain, eth_price_usd=eth_px, onchain_idle_eth_usd=round(eth_onchain * eth_px),
               bridge_usd=round(bridge), in_flight_items=in_flight_items)
    d = pct_diff(nav["syncrone_nav_usd"], bridge) if strat_rows else None
    nav["syncrone_vs_bridge_diff"] = d
    if d is not None and d > TOL:
        flags.append(f"NAV: Syncrone ${nav['syncrone_nav_usd']:,.0f} vs Strategy API ${strat_total:,.0f} + untracked ${untracked_total:,.0f} + in-flight ${sync_in_flight:,.0f} + idle ${sync_idle:,.0f} = ${bridge:,.0f} differ by {d:.2%}. STOP and find the missing position before computing any split or cap.")
    if in_flight_items:
        flags.append(f"NOTE: ${sync_in_flight:,.0f} is in-flight (withdrawal queues) per Syncrone and invisible to Safe/Etherscan/Strategy API: "
                     + "; ".join(f"{i['balance']:,.2f} {i['asset']} in {i['protocol']}" for i in in_flight_items) + ".")
    if untracked:
        flags.append("NOTE: positions the Strategy API / vaults.fyi does not price (kept in the book with APY unknown): "
                     + ", ".join(f"{p} ${v:,.0f}" for p, v in sorted(untracked.items(), key=lambda kv: -kv[1])) + ".")
    return dict(tokens=token_checks, protocols=proto_checks), nav, flags


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--period", default="7day", choices=["1h", "1day", "7day", "30day"])
    ap.add_argument("--chain", type=int, default=None, help="chain id (default: first in registry)")
    ap.add_argument("--skip", nargs="*", default=[], choices=["syncrone", "strategy", "safe", "etherscan"])
    a = ap.parse_args()
    load_env()
    reg = registry()
    c = client(a.client)
    chain_id = a.chain or c["chains"][0]
    safe = c["safes"].get(str(chain_id), {}).get("avatar")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    print(f"[{a.client}] chain {chain_id} avatar {safe}")

    sync_org = None if "syncrone" in a.skip else fetch_syncrone(c, reg)
    sync_all = syncrone_rows(sync_org, reg) if sync_org else []
    sync_rows, sync_other = scope_syncrone(sync_all, safe, chain_id)
    other_wallets = {}
    for r in sync_other:
        k = f"{r['wallet']} chain {r.get('chain')}"
        other_wallets[k] = other_wallets.get(k, 0.0) + (r["usd"] if not r.get("spam") else 0.0)
    if sync_org:
        write_json(out / "raw_syncrone.json", sync_org)
        print(f"  syncrone: {len(sync_rows)} rows in scope ({len(sync_other)} rows in other wallets/chains), window {sync_org['_window']}")
        for k, v in sorted(other_wallets.items(), key=lambda kv: -kv[1]):
            if v > DUST_USD:
                print(f"    other in org: {k} ${v:,.0f}")
    else:
        print("  syncrone: skipped / not configured")

    strat, strat_note = None, None
    if "strategy" not in a.skip:
        try:
            strat = fetch_strategy_current(c, reg, chain_id, a.period)
        except Exception as e:
            # Off the office network: the book is Syncrone's live positions (so NAV ties by construction);
            # APYs are matched from the last snapshot's permitted venues in assess.py and marked stale.
            strat_note = f"Strategy API unreachable ({str(e)[:80]}); positions from Syncrone, APYs from the last snapshot's permitted venues (stale) and fallback sources"
            print("  strategy api:", strat_note)
    strat_rows = strategy_rows(strat, chain_id) if strat else []
    if strat:
        write_json(out / "raw_strategy_current.json", strat)
        print(f"  strategy api: {len(strat_rows)} positions, total ${fnum(strat['summary']['totalValueUsd']):,.0f}, "
              f"vault data at {strat.get('vaultDataFetchedAt')}")
    elif not strat_note:
        print("  strategy api: skipped / not configured")

    safe_rows = []
    if safe and "safe" not in a.skip:
        safe_rows = fetch_safe_balances(reg, chain_id, safe)
        print(f"  safe: {len(safe_rows)} token balances")

    # Wallet balances: the Safe is live, Syncrone books with a delay. Re-base idle rows on the Safe
    # balance at Syncrone's price so a deposit made minutes ago no longer shows as idle.
    if safe_rows and sync_rows:
        by_tok = {r["token"]: r for r in safe_rows}
        lag = []
        for r in sync_rows:
            if r["kind"] != "idle" or not r.get("price"):
                continue
            sb = by_tok.get(r["token"])
            if sb is None:
                continue
            if abs(sb["balance"] - r["balance"]) > max(1e-9, 0.005 * max(sb["balance"], r["balance"])):
                lag.append(f"{r['symbol']} {r['balance']:,.4f} -> {sb['balance']:,.4f}")
                r["balance"], r["usd"] = sb["balance"], sb["balance"] * r["price"]
                r["spam"] = r["usd"] < DUST_USD
                r["rebased_on_safe"] = True
        if lag:
            print("  idle re-based on live Safe balances (Syncrone lagging):", "; ".join(lag[:6]))
    eth_rows = []
    if safe and "etherscan" not in a.skip:
        tokens = {r["token"]: r["decimals"] for r in safe_rows}
        for r in sync_rows:
            if r["token"] and r["token"] not in tokens and (r["kind"] == "position" or not r["spam"]):
                tokens[r["token"]] = 18
        for r in strat_rows:
            tokens.setdefault(r["vault"], 18)
        eth_rows = fetch_etherscan(chain_id, safe, tokens)
        print(f"  etherscan: ETH {eth_rows[0]['balance']:.4f} + {len(eth_rows)-1} token reads")

    priced_safe = []
    if not sync_rows and not strat_rows and safe_rows:
        priced_safe = price_safe_balances(chain_id, safe_rows, reg)
        n_priced = sum(1 for r in priced_safe if r["priced"])
        print(f"  coingecko: priced {n_priced}/{len(priced_safe)} Safe tokens; ${sum(r['usd'] for r in priced_safe):,.0f} total (receipt tokens like aTokens/vault shares are usually unpriced here)")
        sync_rows = priced_safe  # reuse the idle-holding path downstream
    recon, nav, flags = reconcile(sync_rows, strat_rows, safe_rows, eth_rows, reg, c)
    rb = [r for r in sync_rows if r.get("rebased_on_safe")]
    if rb:
        flags.append("NOTE: idle balances taken from the live Safe (Syncrone had not booked recent moves): "
                     + ", ".join(f"{r['symbol']} {r['balance']:,.2f}" for r in rb[:6]))
    if strat_note:
        flags.insert(0, "NOTE: " + strat_note)
    if priced_safe:
        unp = [r["symbol"] for r in priced_safe if not r["priced"] and r["balance"] > 0]
        flags.append(f"NOTE: no Syncrone org or Strategy API client for {a.client}; book = Safe balances priced by CoinGecko. "
                     f"Unpriced tokens ({len(unp)}): {', '.join(str(s) for s in unp[:15])}{' ...' if len(unp) > 15 else ''}")
    positions = sync_rows + strat_rows
    write_json(out / "holdings.json", dict(
        client=a.client, display_name=c["display_name"], chain_id=chain_id, avatar_safe=safe,
        as_of=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), period=a.period,
        sources=dict(syncrone=bool(sync_org), strategy_api=bool(strat), safe=bool(safe_rows), etherscan=bool(eth_rows)),
        positions=positions, other_wallets_in_syncrone_org={k: round(v) for k, v in other_wallets.items() if v > DUST_USD},
        safe_balances=safe_rows, etherscan_balances=eth_rows,
        nav=nav, reconciliation=recon, flags=flags))
    dd = nav["syncrone_vs_bridge_diff"]
    print(f"  NAV syncrone ${nav['syncrone_nav_usd']:,.0f} | bridge (strategy + untracked + in-flight + idle) ${nav['bridge_usd']:,.0f}"
          f" | diff {'n/a' if dd is None else f'{dd:.2%}'}")
    for f in flags:
        print("  FLAG:", f)
    print(f"DONE -> {out / 'holdings.json'}")
    return 0 if not any(f.startswith("NAV:") for f in flags) else 2


if __name__ == "__main__":
    sys.exit(main())
