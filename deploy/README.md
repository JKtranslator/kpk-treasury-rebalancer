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
  to the executor on `127.0.0.1:8743`. `/plan`, `/propose` and `/refresh` require `Authorization: Bearer
  $EXECUTOR_TOKEN` (`.env.local` on the box); the page asks for the token once and keeps it in the browser.
  The GitHub Pages site talks to `https://82-70-94-93.sslip.io` directly, no tunnel. `connect-oci.ps1`
  remains as a fallback.
- `--hourly-live` refreshes live holdings for all clients every hour and pushes `data/` to the repo,
  so GitHub Pages updates without the Action (the Action stays as a fallback if you set its secrets).
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
