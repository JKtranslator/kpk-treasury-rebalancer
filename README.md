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

## Method

`METHOD.md` is the operating procedure Claude follows when asked to `run`: reconcile first, then
policy, then yield within permissions, then the recommendation. `references/` has the detail.
