# Rebalancer service

Cross-client engine behind `run` (defined in the workspace root `CLAUDE.md`). Serves ens, nexus, cow and
balancer; client knowledge comes from the registry, never from this code. Method: `METHOD.md`; data roles:
`references/data-sources.md`; output shape: `references/output-format.md`. Scripts in `scripts/`, outputs
in `runs/<client>/<date>/`. Exit code 2 from `run.py` means the NAV bridge did not tie: stop.

Registry: `clients.json` here is the copy the OCI box reads from the repo; the workspace master is
`kpk/registry/clients.json`. Keep them identical until the generator lands (see `kpk/registry/DIFF-2026-09-14.md`).
Keys via `scripts/common.py`, which reads the shared `%USERPROFILE%\.kpk\env` first.

Single-writer rule for `data/`: this side commits only `data/*.strategy.json`; the OCI box writes
`data/<client>.json`, `data/<client>.live.json`, `data/index.json`. The deny hook blocks staging those.
The executor (`scripts/executor.py`) imports the proposer code from `../../Codex/SafeAgentAll`.
This folder is a git repo (JKtranslator/kpk-treasury-rebalancer); `CLAUDE.md` and the path edits from the
2026-09-14 moves are uncommitted until the user says so.
