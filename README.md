# KPK Treasury Rebalancer

Holdings, policy checks and permitted yield moves for the treasuries KPK manages — **ENS Endowment, Nexus
Mutual, CoW DAO, Balancer DAO** — with a browser simulator for thresholds, exclusions and move sizing.

Prepared by **KPK Treasury**. Internal working view; nothing here is a recommendation until reviewed, and
nothing on the page signs a transaction.

---

## Getting in

The page is served by the executor at **https://82-70-94-93.sslip.io** and asks for the executor token
before it loads anything. The token is stored in your browser, so you are asked once per device; the
executor refuses the snapshot files without it too, so the gate is not just a screen. It lives on the box
in `~/kpk-treasury-rebalancer/.env.local` as `EXECUTOR_TOKEN`.

> The GitHub Pages copy of this repository is public and serves the same `data/` files without a token.
> The token gates the executor, not the internet.

## The two lanes

| | Stored snapshot | Live view |
|---|---|---|
| Endpoint | `/refresh/<client>`, 1–3 min | `/live/<client>`, 2–4 s |
| Page control | *full refresh* link | **Refresh client** |
| Positions | Syncrone, reconciled against the Safe and Etherscan | Safe units × the chain's own rate |
| APYs | vaults.fyi by vault address, DeFiLlama for the rest | vaults.fyi, in parallel |
| Written to | `data/<client>.json`, committed and pushed | nothing; built in memory, cached 10 min |

The stored snapshot is the audited record: it ties NAV three ways and stops (exit code 2) if the bridge does
not. The live view answers "what does the Safe hold right now and which moves still stand" — it takes
permissions, the Roles gate and the policy block from the last snapshot and re-values everything else.

**How a position is valued live.** Each position carries the receipt token the Safe holds and what one unit
converts to: an ERC-4626 share (Fluid, Morpho/kpk, sUSDS) is worth `convertToAssets()` of its underlying,
read on chain; an Aave aToken or Compound Comet balance is its underlying one-for-one; a StakeWise-style
vault with no token is read with `getShares`. The ETH sleeve is re-priced from the Chainlink feed. A receipt
balance that has gone to zero is an exited position and drops out. Positions with no resolvable receipt
(Nexus NXM staking, Uniswap v3 LPs) keep their stored value and are labelled stale.

## Rates

**vaults.fyi is the source of truth.** DeFiLlama only prices what vaults.fyi has no entry for, and never
overrides it. Two things the page does that a protocol dashboard does not:

- **The basis is a 7-day trailing average**, selectable (spot 1-day / 7-day / 30-day). A protocol UI shows
  the rate *right now* — Compound's "Net Supply APR" is the instantaneous contract rate — which is why the
  numbers differ in both directions. Hover any APY to see spot / 7d / 30d together.
- **A venue denominated in a yield-bearing asset earns twice.** Gearbox's wstETH market pays 0.67% *in
  wstETH* while the wstETH keeps earning Lido's 2.28%, so the venue returns 2.96% in ETH terms. vaults.fyi's
  `apyComposite` carries both halves and the page shows them.

## Sizing a move

- **Dilution.** Supplying into a lending market spreads the same borrower interest over a bigger base, so a
  deposit is priced at `r × TVL / (TVL + what this plan adds)` — the rate we would actually receive — and the
  drag on what we already hold there is subtracted from the pickup.
- **Venue ceiling: 20% of a pool** including what we already hold. KPK's working rule, not an IPS one, and
  the default for all four clients. The holdings table shows each position's share of its pool.
- **Protocol cap** from the client's policy block (ENS: 30% of NAV), shared across every venue of that protocol.
- **Roles gate.** Every candidate is checked against the Safe's on-chain Roles permissions. A venue the
  Strategy API lists as permitted but the Safe cannot call yet (pending PUR) is never proposed.

The Rebalancing section has two blocks that each keep their own ledger: **within a category** (same asset,
better venue) and **between categories** (the mandate rotation, pre-sized to what the policy asks for). A
proposal reserves no capacity from the other block unless you tick *Treat this rotation as already executed*;
where they would claim the same room, the page says so in dollars.

## Execute

The **Execute** and **Swap** buttons talk to the executor, which reuses the kpk proposer bot's code — same
parser grammar, permission engine, builders, Tenderly simulation and Safe proposal as the Telegram flow,
split into preview and approve. `/plan` builds and simulates and sends nothing; `/propose` proposes to the
Safe only after you confirm in the popup. The executor reads each client's SafeAgent `.env`; the page never
sees a key.

The **pending box** reads the Safe's own queue — the manager Safe that signs and calls the Roles modifier, as
well as the avatar — so a transaction proposed from the bot appears next to one proposed here, tagged by
source and named by what it does rather than `multiSend`.

## Layout

```
index.html                  the page (token gate, then the views)
assets/app.js               rendering, the move simulator, the swap panel — all client-side
assets/rebalancer.css       page styling;  assets/styles.css  KPK brand base
data/<client>.json          stored snapshot: book with receipts and marks, permitted venues with live
                            APY/TVL, policy checks, the Roles gate, the run's price map
data/index.json             client list for the tabs
clients.json                registry mirror: Safes (avatar, manager, Roles modifier), Syncrone orgs, policies
scripts/run.py              the pipeline: fetch_holdings -> fetch_yields -> assess
scripts/fetch_holdings.py   Syncrone + Safe + Etherscan, reconciliation, on-chain rewards
scripts/fetch_yields.py     permissions, the Roles gate, vaults.fyi then DeFiLlama
scripts/receipts.py         receipt token and unit mark per position (what the live lane values from)
scripts/assess.py           book, policy checks, candidate moves, rewards sweep
scripts/live_lane.py        the fast lane
scripts/publish.py          writes data/<client>.json and data/index.json
scripts/executor.py         HTTP executor on the box: /plan /propose /refresh /live /queue /swap-pairs …
scripts/stamp_assets.py     content-hash ?v= on the asset links (run before committing page changes)
deploy/                     systemd unit, Caddyfile, executor README
references/                 method notes: data sources, permissions, policy checks, output format
```

`.github/workflows/refresh.yml` is **disabled** — it once refreshed live holdings hourly, but it wrote the
same files the box writes and a merged JSON is corrupt JSON. The box is the single writer for `data/`.

## Running the pipeline

```bash
python scripts/run.py --client ens            # writes runs/ens/<date>/assessment.{json,md}
python scripts/publish.py                     # writes data/<client>.json + data/index.json
python scripts/stamp_assets.py                # only when index.html/assets changed
```

Keys are read from the environment, then `%USERPROFILE%\.kpk\env`, then `.env.local` (never committed):
`SYNCRONE_API_KEY`, `ETHERSCAN_API_KEY`, `SAFE_API_KEY`, `VAULTS_FYI_API_KEY`, `EXECUTOR_TOKEN`.
`DEBANK_ACCESS_KEY` is optional and used only for the opt-in cross-check (`DEBANK_CROSSCHECK=1`).

On the box the whole cycle is one call per client: `curl -H "Authorization: Bearer $EXECUTOR_TOKEN"
http://127.0.0.1:8743/refresh/<client>`, which runs the pipeline, publishes and pushes.

## Method

`METHOD.md` is the operating procedure: reconcile first, then policy, then yield within permissions, then
the recommendation. `references/` has the detail; `deploy/README.md` covers the box.
