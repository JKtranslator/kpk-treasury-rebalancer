"""Receipt tokens: what the Safe actually holds for each booked position, and what one unit was worth at the run.

Syncrone is the base record; the Safe Transaction Service tells us what moved. To join the two the snapshot
needs, per position, the token whose balance in the Safe *is* the position (aEthUSDC, fUSDC, cUSDCv3, weETH,
ETHx, kpk vault shares, sUSDS...) and the USD value of one unit at the time of the run. The live lane then
values a position as `Safe units now x unit mark at run`, re-pricing the ETH sleeve from the Chainlink feed.
Marks drift only by the venue's own accrual between runs (a few bps a day), which is well inside what a
"what does the Safe hold right now" view needs; the full pipeline remains the audited record.

Three ways a position is resolved, tried in order:
  erc20   the Strategy API vault address is itself a token the Safe holds (Aave aToken, Fluid fToken,
          Compound cToken, Morpho/kpk vault shares, sUSDS, stETH, ETHx)
  erc20   Syncrone books the position under a token the Safe holds (ether.fi: the Safe holds weETH, the
          Strategy API names the eETH pool)
  shares  neither: vault shares without a token (StakeWise v3) read on-chain with getShares(address)
"""
from __future__ import annotations

from common import fnum

SEL_GET_SHARES = "0xf04da65b"      # getShares(address)   (StakeWise v3 vaults)
SEL_LATEST_ANSWER = "0x50d25bcd"   # latestAnswer()       (Chainlink aggregator, 8 decimals)
CHAINLINK_ETH_USD = {1: "0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419", 100: "0xa767f745331D267c7751297D982b050c93985627"}


def _get_shares(chain_id: int, vault: str, safe: str) -> float:
    from fetch_holdings import _rpc_call
    return int(_rpc_call(chain_id, vault, SEL_GET_SHARES + safe[2:].lower().rjust(64, "0")), 16) / 1e18


def resolve_receipts(book: list[dict], h: dict, reg: dict, permitted: list[dict] | None = None) -> dict:
    """Annotate the assessment book in place (receipt, receipt_method, unit_usd, units_at_run) and return the
    run's price map {token: {symbol, price, asset_group}} for idle tokens and reward pricing."""
    aliases = reg.get("protocol_aliases", {})
    norm = lambda p: aliases.get((p or "").lower(), (p or "").lower()).lower()   # Syncrone name or Strategy API key, one spelling
    ZERO = "0x0000000000000000000000000000000000000000"
    safe = {r["token"]: r for r in h.get("safe_balances") or [] if fnum(r.get("balance")) > 0 and r["token"] != ZERO}   # native ETH is never a receipt
    strat = {(r.get("vault") or "").lower(): r for r in h["positions"] if r["source"] == "strategy_api" and r.get("vault")}
    sync_pos = [r for r in h["positions"] if r["source"] == "syncrone" and r["kind"] == "position" and fnum(r.get("balance")) > 0 and r.get("token")]
    prices = {}
    for r in h["positions"]:
        if r["source"] in ("syncrone", "coingecko") and r.get("token") and fnum(r.get("price")) > 0 and not r.get("spam"):
            prices[r["token"]] = dict(symbol=r.get("symbol"), price=fnum(r["price"]), asset_group=r.get("asset_group"))
    used = set()
    for b in book:
        if b["kind"] != "position" or fnum(b.get("usd")) <= 0:
            continue
        proto, vault = norm(b["protocol"]), (b.get("vault") or "").lower()
        hit = None
        # a receipt is a token whose balance is the position: no unit of one is worth more than $100k (a dust balance of
        # the vault token beside the real receipt, e.g. eETH next to weETH, would otherwise pass with an absurd mark)
        if vault in safe and fnum(b["usd"]) / fnum(safe[vault]["balance"]) < 1e5:
            hit = dict(receipt=vault, receipt_method="erc20", units_at_run=fnum(safe[vault]["balance"]))
        else:                                                              # Syncrone's token for this protocol, held by the Safe
            cands = [r for r in sync_pos if norm(r["protocol"]) == proto and r["token"] in safe and r["token"] not in used
                     and r.get("asset_group") == b.get("asset_group")
                     and abs(fnum(safe[r["token"]]["balance"]) / fnum(r["balance"]) - 1) < 0.02]    # Safe holds what Syncrone books
            if cands:
                r = min(cands, key=lambda r: abs(fnum(r["usd"]) - fnum(b["usd"])))
                if fnum(r["usd"]) > 0 and abs(fnum(r["usd"]) / fnum(b["usd"]) - 1) < 0.10:
                    hit = dict(receipt=r["token"], receipt_method="erc20", units_at_run=fnum(safe[r["token"]]["balance"]))
        if not hit and not vault:                                          # Syncrone-only row: the protocol's single Strategy API vault
            cands = [r for r in strat.values() if norm(r["protocol"]) == proto and r.get("asset_group") == b.get("asset_group") and fnum(r.get("usd")) > 0]
            if len(cands) == 1:
                vault = cands[0]["vault"]
                if vault in safe and fnum(b["usd"]) / fnum(safe[vault]["balance"]) < 1e5:
                    hit = dict(receipt=vault, receipt_method="erc20", units_at_run=fnum(safe[vault]["balance"]))
        if not hit and vault in strat and strat[vault].get("lp_balance_raw"):
            hit = dict(receipt=vault, receipt_method="shares", units_at_run=int(strat[vault]["lp_balance_raw"]) / 1e18)
        if not hit and vault and vault not in safe and h.get("avatar_safe"):
            # a vault the Safe holds no token of (StakeWise v3): shares read on-chain at publish time
            try:
                sh = _get_shares(int(h["chain_id"]), vault, h["avatar_safe"])
            except Exception as e:
                sh = 0.0; print(f"  receipts: getShares {vault[:10]}: {str(e)[:80]}")
            if sh > 0:
                hit = dict(receipt=vault, receipt_method="shares", units_at_run=sh)
        if not hit and not vault and permitted and h.get("avatar_safe"):
            # no Strategy API row either (off-network run): the protocol's single permitted vault, shares read on-chain
            cands = {(p.get("vault") or "").lower() for p in permitted if norm(p.get("protocol")) == proto
                     and p.get("asset_group") == b.get("asset_group") and p.get("vault") and p.get("action") in ("deposit", "stake")}
            if len(cands) == 1:
                v = cands.pop()
                try:
                    sh = _get_shares(int(h["chain_id"]), v, h["avatar_safe"])
                except Exception as e:
                    sh = 0.0; print(f"  receipts: getShares {v[:10]}: {str(e)[:80]}")
                if sh > 0:
                    hit = dict(receipt=v, receipt_method="shares", units_at_run=sh)
        if hit and hit["units_at_run"] > 0:
            hit["unit_usd"] = fnum(b["usd"]) / hit["units_at_run"]
            used.add(hit["receipt"])
            b.update(hit)
    return prices
