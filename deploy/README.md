# Executor on the OCI box

Runs next to the kpkProposer bot (`~/SafeAgentAll`) on `ubuntu@82.70.94.93`, using its venv so the
executor worker imports the bot's own builders, permission engine and Tenderly/Safe code.

```
~/kpk-treasury-rebalancer        git clone of JKtranslator/kpk-treasury-rebalancer (this repo)
  .env.local                     SYNCRONE_API_KEY, ETHERSCAN_API_KEY, SAFE_API_KEY (0600, never committed)
  runs/                          run outputs, executor plans, logs
/etc/systemd/system/kpk-rebalancer.service   from deploy/kpk-rebalancer.service
```

- Binds `127.0.0.1:8743` only. Reach it through `connect-oci.ps1` (SSH tunnel) from a machine the box
  allows; the page then talks to `http://127.0.0.1:8743` exactly as with a local executor.
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
