# Permissions per client

A move can only be proposed if the client's Zodiac Roles Modifier allows it for the MANAGER role.
Two views exist:

1. **Strategy API `permissions`** (`fetch_yields.py` → `yields.json[permitted]`): protocol / asset /
   action, priced with vaults.fyi where possible. Convenient, current as of `vaultDataFetchedAt`,
   but it is a derived view.
2. **On-chain scrape** by SafeAgent: `Codex/SafeAgentAll/<client folder>/Data/live_permissions.json`
   (`{target_address: {selector: options}}`), refreshed by `shared/tools/fetch_permissions.py`, parsed
   into protocol → action → targets by each client's `parser_permissions.py`, gated at build time by
   `shared/core/permission_engine.py::is_allowed`. Human-readable matrix:
   `<client folder>/onchain_matrix_report.md`. This is the authoritative allow-list.

Client folders under `Codex/SafeAgentAll/` (canonical tree, 2026-09-11): `ens`, `nexus`, `cow dao`
(main + Gnosis + two defence Safes), `balancer` and `balancer gnosis`. Other folders there (`arbitrum`, `kpk mainnet`, `OIV`) are not clients of this skill. Role key `MANAGER` =
`0x4d414e414745520000…`.

## Roles Modifier and Safes

| Client | Chain | Avatar Safe | Roles Modifier | Manager Safe |
|---|---|---|---|---|
| ENS | 1 | `0x4F2083f5fBede34C2714aFfb3105539775f7FE64` | `0x703806e61847984346d2d7ddd853049627e50a40` | `0xb423e0f6E7430fa29500c5cC9bd83D28c8BD8978` |
| Nexus Mutual | 1 | `0x8e53D04644E9ab0412a8c6bd228C84da7664cFE3` | `0x24Fc0B9414Bb8e99a822a45BdE3A398BcfC85A58` | `0x0F035E7ff8A21159F10994aA0186449fc852A9E7` |
| CoW DAO main | 1, 100 | `0x616dE58c011F8736fa20c7Ae5352F7f6FB9F0669` | `0xaF65eec285e2999e8f7bFEc57E83e88A9525F1E6` | `0xfABCA7957D67c680d3ab2C28b8dC003A5852123a` |
| CoW DAO defence | 1, 100 | `0x7F8987D6A8bee31bD7bE80E877732579E2582a28` | `0xF3eDA195de9d0262a1A5177F5c663930e53E1c5c` | same |
| Balancer | 1, 100 | `0x0EFcCBb9E2C09Ea29551879bd9Da32362b32fc89` | `0x13c61a25DB73e7a94a244bD2205aDba8b4a60F4a` | `0x60716991aCDA9E990bFB3b1224f1f0fB81538267` |

The stale `Claude/SafeAgentAll/karpatkey/.env` still carries the old CoW Roles Modifier
`0xdc62…baff7`; ignore it.

## Permitted surface (Strategy API view, 2026-09-11, mainnet)

Entries with an APY are priced by vaults.fyi; the rest are permitted but must be priced by hand.

**ENS** — aave_v3 (DAI, ETH, ETHx, osETH, USDC, USDS, USDT, WETH deposit), compound_v3 (cUSDCv3,
cUSDSv3, cUSDTv3), ether_fi (eETH swap), stakewise_v3 (osETH swap), spark (sUSDS swap; ETH stake;
ETH, USDC, USDS, USDT, WETH, wstETH deposit), fluid (GHO, USDC, USDT), lido (stETH), morphoVaults
(kpk ETH/USDC/USDT Prime v1+v2, kpk ETH Yield v2, kpk USDC Yield v2, Sentora PYUSD/RLUSD, bbqUSDC,
Steakhouse High Yield USDC), rocket_pool (rETH), sky (USDS), stader (ETHx). On-chain matrix adds
Gearbox, Balancer, Aura, Convex, Curve, Uniswap v3, CoW swaps, Merkl claims and a whitelisted
transfer to `wallet.ensdao.eth` (`0xFe89cc7aBB2C4183683ab71653C4cdc9B02D44b7`, ETH + USDC).

**Nexus Mutual** — aave_v3, compound_v3, spark, fluid, lido, gearbox (kpk wstETH, kpk WETH),
stakewise_v3, ether_fi, morphoVaults (kpk vaults incl. USDC Yield v1/v2, Gauntlet USDC Prime),
sky. On-chain adds Balancer, Aura, Curve, Uniswap v3, CoW, Merkl, Aave Umbrella, GHO. Nexus Mutual
staking (NXM pools, RWIV) is held but is not in the yield universe: assess it separately.

**CoW DAO main** — aave_v3 (USDC, USDT, EURC, WETH, sDAI), compound_v3 (cUSDCv3, cWBTCv3,
cWstETHv3), morphoVaults (kpk USDC Prime v1/v2 + Core, kpk ETH Prime v1/v2, kpk EURC Yield
v1/v2), spark (DAI, USDS, USDC/USDT via Sky, WBTC), sky, lido, ether_fi, stakewise_v3,
rocket_pool, gearbox, fluid. Gnosis profile: Aave (EURe, xDAI, sDAI), Savings xDAI, CoW. The
defence Safes are separate Roles instances; run them as their own scope.

**Balancer** — Balancer pools (`balancer_pools.py`), aave_v3, compound_v3, morphoVaults (kpk
USDC Prime v2, kpk USDC Yield v2, kpk ETH Yield v2, kpk EURC Yield v2, Core), spark, sky, lido,
rocket_pool, ether_fi, stakewise_v3 (Gnosis osGNO vaults too), gearbox, fluid, Angle, Umbrella,
Merkl, CoW, Savings xDAI (Gnosis).

## Checking a proposed move against the on-chain allow-list

```bash
python "Codex/SafeAgentAll/shared/tools/fetch_permissions.py" --client ens     # refresh
python - <<'EOF'
import json; d=json.load(open("Codex/SafeAgentAll/ens/Data/live_permissions.json"))
target="0xd5cce260e7a755ddf0fb9cdf06443d593aaeaa13"  # kpk USDC Yield V2
print(d.get(target.lower()) or "NOT PERMITTED")
EOF
```

If the target is missing, the move needs a permissions update (PUR) first; say so in the
recommendation and do not size it as an action.
