# Executor on the OCI box

Runs on `ubuntu@82.70.94.93`. The executor worker imports the kpkProposer code from a tracked checkout of
**kpk-labs/kpk-proposer** at `~/kpk-proposer` (read-only deploy key) and fast-forwards it to `origin/main`
before every plan and refresh, so a merged PR on kpk-labs is live on the next click. The page shows the
commit it built with. Runtime files the repo does not carry (`.env` per client, `.venv`) are local to the box.

```
~/kpk-treasury-rebalancer        git clone of JKtranslator/kpk-treasury-rebalancer (this repo)
  .env.local                     SYNCRONE_API_KEY, ETHERSCAN_API_KEY, SAFE_API_KEY (0600, never committed)
  runs/                          run outputs, executor plans, logs
/etc/systemd/system/kpk-rebalancer.service   from deploy/kpk-rebalancer.service
```

- Public HTTPS: Caddy on 443 (`deploy/Caddyfile`, Let's Encrypt via `82-70-94-93.sslip.io`) reverse-proxies
  to the executor on `127.0.0.1:8743`. Everything but `/health` requires `Authorization: Bearer
  $EXECUTOR_TOKEN` (`.env.local` on the box) — `/plan`, `/propose`, `/refresh/`, `/live/`, `/queue/`,
  `/auth` **and `/data/`**, so the snapshots themselves are behind the token, not just the buttons. The page
  asks for it before it renders anything and keeps it in the browser, so it is asked once per device.
  `connect-oci.ps1` remains as a fallback.
- A caller counts as local only on a loopback socket **with no `X-Forwarded-*`/`Forwarded`/`X-Real-IP` and no
  `Origin` header**. Caddy proxies every public request from 127.0.0.1, so the older "loopback without Origin"
  rule let any script through the public URL; the box's own `curl http://127.0.0.1:8743/refresh/<client>`
  still works without a token.
- **The GitHub Pages copy of the repo is public** and serves the same `data/` files with no token. The gate
  protects the executor, not the internet.
- `--hourly-live` refreshes live holdings for all clients every hour and pushes `data/` to the repo.
  `.github/workflows/refresh.yml` is disabled: it wrote the same files, and a merged JSON is corrupt JSON.
  The box is the single writer for `data/`; the office side commits only `data/*.strategy.json`.
- Pushes use a repo deploy key generated on the box (`~/.ssh/kpk_rebalancer_deploy`), write access.
- The box cannot reach `ops-tools-backend.kpk`. Permissions and vaults.fyi APYs are reused from the
  last snapshot pushed by a local `run` on the office network and are marked stale on the page.

Ops:

```bash
ssh -i ~/.ssh/oracle_pmkt ubuntu@82.70.94.93
sudo systemctl status kpk-rebalancer.service --no-pager
tail -50 ~/kpk-treasury-rebalancer/runs/executor.stderr.log
cd ~/kpk-treasury-rebalancer && git pull --rebase && sudo systemctl restart kpk-rebalancer.service
```

## Deploying page changes

After editing anything under `assets/`, run `python scripts/stamp_assets.py` before committing: it rewrites the `?v=` on the asset links in `index.html` with a content hash so browsers and GitHub Pages fetch the new files. Without it users keep the cached `app.js` and see stale behaviour (the box only stamps data files, never the page).

## Keys the box needs (names only, values in .env.local)

`SYNCRONE_API_KEY`, `SAFE_API_KEY`, `ETHERSCAN_API_KEY`, `EXECUTOR_TOKEN`, plus since 2026-09-15 `VAULTS_FYI_API_KEY` (first APY tier, read live per vault address) and `DEBANK_ACCESS_KEY` (independent position cross-check in the reconciliation). Locally they live in the shared secrets file under the user profile.

## Roles gate

`fetch_yields.py` verifies every vault-type venue in the Strategy API permitted list against the proposer bot's parsed on-chain Roles (`<kpk-proposer>/<client dir>/Data/live_permissions.json`, path from `safeagent_dir` in the registry). Venues not in Roles are marked `NOT IN ROLES`, unpriced, and can never be proposed by either engine. The Strategy API list can run ahead of the Roles (pending PURs); only the Roles file counts.

## Fast lane (`/live/<client>`)

The Refresh button on the page calls `GET /live/<client>` (bearer token). `scripts/live_lane.py` rebuilds a snapshot-shaped view in a few seconds from the Safe Transaction Service (token units now), the last run's marks (`scripts/receipts.py`: receipt token and USD per unit for every position, Syncrone as base), Chainlink ETH/USD over public RPC for the ETH sleeve, and vaults.fyi (APY per vault, parallel, 3 CU each). No DeBank: a position is `Safe units now x unit mark at run`; a receipt balance at zero is an exited position; a new receipt token is flagged for a full refresh, runs the same policy / performance / rewards-sweep engines as the pipeline, and returns it. Permissions, the Roles gate and the policy block come from the last published `data/<client>.json`. Cached 10 minutes per client in the executor process (Refresh forces a rebuild at most once a minute; an executed tracked Safe transaction drops the cache). The full pipeline (`/refresh/<client>`, 1-3 minutes, Syncrone + Etherscan + reconciliation + git push) remains the audited record and sits behind the "full refresh" link.

`GET /safe-tx/<client>/<safeTxHash>` reports whether a proposal has been executed (Safe Transaction Service). The page records every proposal it makes, polls this every 30 s, and drops the pending card and re-runs the live view once the Safe executes it.

## Pending queue (`/queue/<client>`)

Reads every unexecuted transaction at or above the current nonce from the Safe Transaction Service, for both
the **manager** Safe (where proposals are raised — it signs and calls the Roles modifier) and the avatar.
Asking the avatar alone returns nothing: ENS's manager sits around nonce 930 while its avatar is at 34. Each
row is named by what it does, not by the method the Safe reports — a bot proposal arrives as
`multiSend(execTransactionWithRole, ...)`, so the endpoint unwraps both layers, reads the verb from each
call's selector and takes the venue from the Roles call's target, dropping the `approve` as plumbing. The
page tags rows this page / bot and shows pending only; executed transactions are already in the live view.

## Token gate (`/auth`)

`GET /auth` returns 200 for a valid bearer token and 401 otherwise. The page calls it before rendering: a
token stored in the browser is only good if the executor still accepts it, and an unreachable executor is
reported as such rather than treated as a bad token. Clearing the stored token is a link on the gate.

## Rates: who wins

`vaults.fyi` is the source of truth. `fetch_yields.py` reprices every permitted venue by vault address from
vaults.fyi first; DeFiLlama only touches rows vaults.fyi has no entry for, and the book's fuzzy fallback
prefers a vaults.fyi-priced candidate on a tie. Two details worth knowing:

- Where `apyComposite` is present the venue is denominated in a yield-bearing asset (Gearbox wstETH, Aave
  osETH/ETHx/rETH) and `apyComposite.totalApy` — intrinsic staking compounded with the venue's own rate — is
  the number comparable to everything else in that asset group. The plain `apy` is the venue rate alone and
  reading it made every such market look like a downgrade from simply holding the LST.
- The page's basis is a 7-day trailing average. A protocol dashboard shows the instantaneous rate (verified:
  Compound's Comet `getSupplyRate` at the current utilisation equals its UI figure to the basis point), so
  the two differ in both directions. The APY cell carries spot / 7d / 30d on hover.
