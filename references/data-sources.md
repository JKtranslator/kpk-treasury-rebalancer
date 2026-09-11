# Data sources

Four read-only sources, each with a distinct role. Never let one substitute for another.

| Role | Source | Auth | Blind spots |
|---|---|---|---|
| Accounting book: positions, quantities, prices, PnL, in-flight withdrawals, idle holdings | Syncrone v2 performance API | `SYNCRONE_API_KEY` (header `x-api-key`) | No annualised APY; `apy_pct` is month-to-date. Books some positions in underlying units (StakeWise "ETH", Fluid "USDC"). One org can hold several wallets and chains. |
| On-chain balances of the avatar Safe | Safe Transaction Service | `SAFE_API_KEY` as Bearer on `api.safe.global/tx-service/<chain>`; legacy `safe-transaction-<chain>.safe.global` works without a key | Raw token balances only, no fiat. Shows receipt tokens (aEthUSDC, fUSDC, weETH), so it cannot see staked ETH in StakeWise or NXM staking NFTs. Spam tokens included. |
| Independent on-chain read | Etherscan V2 | `ETHERSCAN_API_KEY`, `chainid=` on every call | Free tier: 5 calls/s; `tag=<block>` is ignored on `eth_call`. Same visibility as Safe. |
| Operational view: vault positions with APY, permitted venues with APY and TVL, optimizer | KPK Strategy API `http://ops-tools-backend.kpk/api` | None (internal network) | Only vaults.fyi-tracked venues on one chain per call. Swap-only permissions (ether.fi eETH) are excluded from the optimizer. Data as of `vaultDataFetchedAt`, refreshed roughly daily. |
| Benchmarks and any unpriced vault (optional) | vaults.fyi v2 `https://api.vaults.fyi/v2` | `VAULTS_FYI_API_KEY` (header `x-api-key`), 402 without one | Credit-metered. |

## Syncrone v2

`GET https://api-v2.syncrone.fi/api/kpk/<org_id>/performance?from=YYYY-MM-01&to=<1st of next month>`
One month per call; the script uses the current month and reads the `final` cut.

Shape: `{start_date, end_date, organizations[{org_name, org_id, metrics, wallets[{wallet_address,
metrics, holdings_summary, holdings[], protocols[{protocol_name, positions[{id, position_name,
position_type, chain_id, assets[{id, token{address,symbol,decimals}, initial, final{balance,
price_usd, balance_usd}, flows, tx_hashes, metrics, children}]}]}]}]}]}`

- Asset `id` encodes the state: `...-stake-...` is deployed, `...-withdraw_process-...` is in a
  withdrawal queue (counted as in-flight, not deployable). Children hold the underlying.
- Org ids: see `clients.json`. Arbitrum was offboarded 2026-08.
- Team scripts with the same pattern: `Claude/Hypernative/syncrone_v2_snapshot.py` (sanctioned),
  `~/.claude/skills/treasury-report-core/scripts/syncrone_api.py` (monthly report path).
  `Claude/Hypernative/syncrone_api.py` is quarantined (fabricated fallbacks); do not copy it.
- Known faults from the reporting pack: `tx_hashes[].amount` is broken (use value/price);
  `net_flow_usd` includes internal rotations; Merkl rows carry null `nav_usd`.

## Safe Transaction Service

`GET <base>/safes/<safe>/balances/?trusted=false&exclude_spam=true` → `[{tokenAddress, token{name,
symbol, decimals, logoUri}, balance}]`; `tokenAddress: null` is native ETH. No fiat values.
`trusted=true` cuts the list to Safe-curated tokens (16 for ENS) and drops fTokens and kpk vault
shares, so the script uses `trusted=false` and names tokens from Syncrone / Strategy API instead.
Base URLs per chain are in `clients.json`. The team's Safe code (`Codex/SafeAgentAll/shared/core/
propose_tx.py`) uses the same Bearer header.

## Etherscan V2

`https://api.etherscan.io/v2/api?chainid=<id>&module=account&action=balance|tokenbalance&address=
<safe>[&contractaddress=<token>]&tag=latest&apikey=<key>`. The script reads ETH plus every token
named by Safe, Syncrone or the Strategy API, then compares Safe vs Etherscan per token. They must
agree exactly (both are on-chain); a difference is a transient or an indexer fault, rerun.

## KPK Strategy API (ops-tools)

Frontend `http://ops-tools.kpk/` (React; routes `/strategy-optimizer`, `/ens-endowment-model`,
`/hypernative-insights`, Google login for the UI). Backend `http://ops-tools-backend.kpk/api`,
answers without a token on the internal network:

| Endpoint | Body | Returns |
|---|---|---|
| `GET /clients` | – | `clients[{name, description, chain, defaultChain, chains[]}]`: balancer-dao, cow (mainnet, gnosis), ens-dao, nexus-mutual |
| `POST /clients/permissions` | `{clientName, chain}` | `permissions{<protocol>: [{asset, chain, action: deposit|stake|swap, vaultsfyiInfo{vault}, apy{1h,1day,7day,30day: {base,reward,total}}, tvl{usd,native}}]}` for the client, `allPermissions` for the whole universe, `vaultDataFetchedAt` |
| `POST /clients/current-strategy` | `{clientName, period: 1h|1day|7day|30day, chain}` | `strategy[{assetGroup USD|ETH|EURO, assets[], apy, totalValueUsd, positions[{asset, protocol, vault, vaultName, apy, positionValueUsd, lpTokenBalance, tvl}]}]`, `summary{totalValueUsd, weightedAverageAPY}` |
| `POST /clients/best-strategy` | `{clientName, period, chain, assetGroupConstraints{}, tvlCapPercent}` | `recommendations[{assetGroup, currentWeightedAPY, recommendedWeightedAPY, allocations[{protocol, vaultName, vault, apy, currentValue, recommendedValue, tvl}]}]` |
| `GET /hypernative/...`, `/admin/data-sync` | – | Hypernative agents/channels; data refresh status (manual resync has a cooldown) |

Facts to remember about it:
- `current-strategy` for ENS (2026-09-11) was $89.56M against Syncrone $92.56M; the gap was
  705 idle ETH plus 500 ETHx in Stader's withdrawal queue, both invisible to it.
- `best-strategy` for ENS proposed 90% of the USD book into Compound v3 and 60% of ETH into
  StakeWise, breaching the IPS 30% cap. It optimises APY under `tvlCapPercent` only.
- Permission entries with `apy: null` are permitted but unpriced (Spark, Morpho Blue markets,
  Balancer/Aura/Convex pools, several kpk vault v1s, CoW swaps). The assessment lists them, never
  ranks them.
- Protocol keys: `aave_v3, compound_v3, ether_fi, stakewise_v3, spark, fluid, lido, morphoVaults,
  morphoMarkets, rocket_pool, sky, stader, gearbox, balancer_v2, aura, convex, curve, cowswap, ankr`.
  `clients.json` `protocol_aliases` maps Syncrone names onto these.
- Source is private (karpatkey GitHub org has no public ops-tools repo). Public kpk repos that
  relate: `karpatkey/defi-kit` (Roles permission presets and `kit.karpatkey.com/api/v1`),
  `karpatkey/client-configs` (per-client Roles configs).

## vaults.fyi (optional)

`GET /v2/benchmarks?network=mainnet&code=usd|eth` (TVL-weighted DeFi benchmark), `GET
/v2/detailed-vaults/<network>/<vault>` (APY, TVL), `GET /v2/positions/<wallet>`. kpk is a listed
curator, so `curator=kpk` filters to the kpk Morpho vaults. There is an MCP server
(`npx @vaultsfyi/mcp`, env `VAULTS_API_KEY`) if you want the tools instead of the REST calls.

## Other team sources worth knowing

- `Claude/Hypernative/client_registry.py` — the same Safe addresses, plus Slack and Hypernative
  channel ids per client.
- `ENS/ens_flows.py` — Safe `all-transactions` routing to positions, to separate rebalances from
  yield when reading Syncrone PnL.
- `Nexus/nexus_capital_pool_model/` — DeFiLlama `yields.llama.fi/chart/<pool>` and Morpho GraphQL
  as keyless APY histories, with a biweekly rebalance backtest harness.
- Dune MCP connector in the Claude session — `dune.com/kpk/kpk-morpho-vaults` for the kpk vault
  TVL/APY series without a Dune key.
