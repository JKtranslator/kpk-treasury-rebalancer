# Rebalancer method

Invoked by typing `run` (or `/run`) in the Karpatkey workspace; see `CLAUDE.md` and `.claude/commands/run.md`.
The scripts gather and check; Claude reads `assessment.md` and writes the recommendation by the steps below.

Produce a rebalancing assessment for a kpk client treasury: what it holds, what each position
earns, whether it sits within the client's policy, and which moves inside the client's Roles
permissions would improve yield. Three independent balance sources are reconciled before any
number is used. The scripts gather and check; you write the recommendation.

## Data & NAV rules (always apply)

- **Syncrone is the accounting book** (positions, quantities, prices, in-flight withdrawals, idle
  holdings). It carries no annualised APY; its `apy_pct` is month-to-date return. Never quote it
  as a yield.
- **Safe Transaction Service and Etherscan V2 are the two on-chain reads.** They must agree with
  each other on every named token. If they disagree, retry; never pick one.
- **The KPK Strategy API is the operational view**: vault positions with vaults.fyi APY, plus the
  client's permitted venues with APY and TVL. It only sees vaults.fyi-tracked venues on one chain,
  so it understates NAV for clients with LP, staking-NFT or Gnosis positions. Those positions stay
  in the book as "untracked, APY unknown".
- **Reconciliation guardrail.** `fetch_holdings.py` bridges Syncrone NAV to Strategy API + untracked
  + in-flight + idle. If the residual is >1% it exits 2 and prints STOP. Do not compute a split,
  a cap or a recommendation until the gap is explained. Block on contradictory data, not on
  missing data.
- **Permissions are the universe.** Only venues in the client's Roles permissions can be proposed.
  The Strategy API `permissions` block is the priced view; the on-chain scrape in
  `Codex/SafeAgentAll/<client>/Data/live_permissions.json` is the authoritative allow-list when the
  two disagree (see `references/permissions.md`).
- **The ops-tools optimizer output is reference only.** It maximises APY under a per-venue TVL cap
  and ignores policy caps, floors and swap-only venues. Never forward it as the recommendation.

## Step 0 — Intake (short; the registry knows the addresses)

Ask only what the registry cannot know:

1. **Which client** (ens, nexus, cow, balancer) and, for multi-chain clients, **which chain**
   (default: first in `clients.json`). Never ask for addresses; they are in `clients.json`.
2. **Exclusions**: venues to avoid this round (hack, blacklist, pending exit). Pass as `--exclude`.
3. **Thresholds** if the user wants them different from the defaults (50 bps pickup, $250k
   minimum move, 10% of venue TVL).
4. **Output**: default is the forwardable doc in `references/output-format.md`; ask about
   denomination (USD vs native units) only if the client is ETH-heavy.

If the client is ENS, load the `ens-ips-2026` skill for the policy text. Do not ask for the policy.

Keys come from the environment, then `<skill>/.env.local`, then
`Claude/Hypernative/.env.local` (the team's existing secrets file). Needed: `SYNCRONE_API_KEY`,
`ETHERSCAN_API_KEY`, optionally `SAFE_API_KEY` (the legacy Safe host works without it) and
`VAULTS_FYI_API_KEY` (benchmarks). The Strategy API needs the office network / Twingate and no key.

## Step 1 — Run the pipeline

```bash
python rebalancer/scripts/run.py --client ens
# options: --chain 100  --period 30day  --exclude gearbox  --min-pickup-bps 75  --min-move-usd 500000
```

Writes `runs/<client>/<date>/`: `holdings.json` (positions from all sources, token and protocol
reconciliation, NAV bridge, flags), `yields.json` (permitted venues with APY/TVL, ops-tools
optimizer output, vaults.fyi benchmarks if keyed), `assessment.json` and **`assessment.md`**.
Exit code 2 = NAV did not tie. Read the flags first.

Runs take 1 to 3 minutes (Etherscan is rate-limited to 5 calls/s). Background it if the client has
many tokens.

## Step 2 — Read `assessment.md` and resolve every flag

- `NOTE: in-flight` items (withdrawal queues) are expected; mention them as not deployable yet.
- `NOTE: untracked` positions have no APY feed. For Nexus that is NXM staking; for CoW and
  Balancer their own protocol positions and Gnosis-chain books. Judge them from protocol data or
  leave them as "not assessed for yield", say so.
- A `NAV: ... STOP` flag means a position exists in one source and not the other. Find it (a new
  venue the Strategy API does not map, a second wallet in the Syncrone org, a stale vaults.fyi
  snapshot: check `vaultDataFetchedAt`) before continuing.
- Safe vs Etherscan disagreement on a named token is an infrastructure fault; rerun.

## Step 3 — Policy (if the client has one)

The script applies the `policy` block in `clients.json` (ENS: floor, effective stable target,
30% single-protocol cap, whitelists). Check its arithmetic against the IPS text in `ens-ips-2026`,
then apply the judgement the script cannot: risk-sleeve classification of any new venue, RWA and
permissioned sub-caps, LSD consensus share, and whether the floor regime authorises off-cycle ETH
sales. `references/policy-checks.md` has the framework. Clients with `policy: null` are pure
yield within permissions.

## Step 4 — Yield (within permissions)

`assessment.md` lists, per asset group: blended APY, the best permitted venues with 7-day and
30-day APY and TVL, permitted venues without an APY feed, idle balances, laggards, and candidate
moves sized to the venue TVL cap and the policy protocol-cap headroom, with $/yr pickup. Apply:

- Idle capital at 0% and any position in an excluded venue are always actions.
- Reward-bearing wrappers (weETH, wstETH, osETH, ETHx) earn via exchange rate even when idle;
  confirm before quoting a pickup.
- Prefer like-for-like rotations over selling the volatile asset, unless the floor regime says
  otherwise.
- Check exit liquidity for lending-market withdrawals (utilisation, available liquidity) before
  sizing; the script does not.
- Unpriced permitted venues (Spark, Morpho markets, some kpk vaults) may beat the priced ones.
  Price them by hand (vaults.fyi, Morpho API, protocol UI) or list them as not assessed.
- Cite 30-day APY when 7-day and 30-day disagree materially; a 7-day spike is not a reason to move.

## Step 5 — Write the recommendation

Use `references/output-format.md`: Summary (NAV, split, floor status, effective target),
Expected impact, `Actions <Asset>` blocks with WITHDRAW / DEPOSIT / DEPLOY / SWAP bullets and
amounts, Rationale, Note with data caveats. Separate parameter rebalance (policy) from
performance rebalance (yield). Every action must be permitted; name the venue exactly as the
permissions list does so the SafeAgent operator can map it to a route.

## Files

- `clients.json` — registry: per-client Strategy API name, Syncrone org, avatar/manager/Roles
  Modifier Safes per chain, policy block, protocol aliases, asset groups. Edit here.
- `scripts/run.py` → `fetch_holdings.py`, `fetch_yields.py`, `assess.py`; `common.py` shared.
- `references/data-sources.md` — endpoints, auth, shapes, and each source's blind spots.
- `references/permissions.md` — where permissions live per client, and what each client may use.
- `references/policy-checks.md` — constraint framework with the ENS IPS 2026 instance.
- `references/output-format.md` — recommendation template and worked examples.
- `runs/` — outputs, dated per client; `runs/plans/` holds executor plans. Not for sharing as-is.
- `scripts/executor.py` + `executor_worker.py` — local executor on the SafeAgent (kpk proposer bot) code for the page's Execute button: preview (build + Tenderly) then propose.
- `index.html`, `assets/`, `data/` — the GitHub Pages site; `scripts/publish.py` and `refresh_holdings.py` feed it.

Forked 2026-09-11 from kpk-treasury 2026.9.1 `treasury-rebalancer`; plain tool folder, not a skill. Edit freely.
