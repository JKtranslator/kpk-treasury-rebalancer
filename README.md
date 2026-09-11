# KPK Treasury Rebalancer

Holdings, policy checks and permitted yield moves for the treasuries KPK manages (ENS Endowment,
Nexus Mutual, CoW DAO, Balancer DAO), with a browser simulator for thresholds and exclusions.

Prepared by **KPK Treasury**. Internal working view; nothing here is a recommendation until reviewed.

---

## How it works

Static site on GitHub Pages, two data lanes:

```
index.html                  the page
assets/app.js               rendering + the move simulator (all client-side)
assets/styles.css           KPK brand styling (shared with the dYdX buyback page)
data/<client>.json          stored snapshot: positions with APY, permitted venues, policy checks,
                            ops-tools optimizer output. Written by a local `run`.
data/<client>.live.json     live holdings: Syncrone + Safe + Etherscan, refreshed hourly by Actions.
scripts/                    fetch_holdings, fetch_yields, assess, run, publish, refresh_holdings
clients.json                registry: Safes, Roles Modifiers, Syncrone orgs, policies
references/                 method notes: data sources, permissions, policy checks, output format
.github/workflows/          refresh.yml — hourly live-holdings refresh
```

- **Stored snapshot** (`run` locally, then push): full pipeline including the KPK Strategy API
  (ops-tools), which sits on the office network. Gives APYs, permitted venues and the optimizer view.
- **Live holdings** (Actions, hourly): NAV, positions by protocol, idle, in-flight withdrawals, and
  the Safe-vs-Etherscan token check. No APY, because the Strategy API is not reachable from GitHub.

The page shows both and flags when live NAV has drifted from the stored snapshot.

## Refresh

```bash
# full snapshot (office network, keys in env or .env.local)
python scripts/run.py --client ens && python scripts/run.py --client nexus
python scripts/publish.py            # writes data/<client>.json
git add data && git commit -m "data: snapshot" && git push

# live holdings only (what the Action runs)
python scripts/refresh_holdings.py
```

Keys: `SYNCRONE_API_KEY`, `ETHERSCAN_API_KEY`, optional `SAFE_API_KEY`, optional `VAULTS_FYI_API_KEY`.
Locally they are read from the environment then `.env.local` (never committed). In Actions they are
repository secrets.

## Execute (local executor on the kpk proposer bot)

The **Execute** button on a simulator move talks to a local executor that reuses the SafeAgent
(kpk proposer bot) code: same parser grammar, permission engine, builders, Tenderly simulation and
Safe Transaction Service proposal as the Telegram flow, split into preview and approve.

```bash
python scripts/executor.py            # http://127.0.0.1:8743 ; needs Codex/SafeAgentAll next to this folder
```

`/plan` builds and simulates (nothing sent); the popup shows the commands, the Roles-wrapped steps,
the permission result and the Tenderly link. `/propose` sends the stored manager transaction to the
Safe for signers, only after you confirm in the popup. Swaps (CoW) and LST exits are refused here and
must go through the bot. The executor reads each client's SafeAgent `.env`; the page never sees keys.

Tenderly links: the bot shares the simulation and builds a `/public/` URL without checking the share
response, so on plans that do not allow public simulations the link lands on Tenderly's home page.
The executor checks the share status, reports the reason, and always returns the private dashboard
URL (needs a Tenderly login).

## Method

`METHOD.md` is the operating procedure Claude follows when asked to `run`: reconcile first, then
policy, then yield within permissions, then the recommendation. `references/` has the detail.
