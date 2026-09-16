#!/usr/bin/env bash
# Re-place a CoW order that a Safe transaction has already pre-signed on-chain but that never reached the CoW API.
# Usage (on the box): bash ~/kpk-treasury-rebalancer/deploy/resubmit.sh <client> <tx_hash> [--dry-run]
set -euo pipefail
CLIENT="${1:?client slug (ens|nexus|cow|balancer)}"; TX="${2:?transaction hash}"; DRY="${3:-}"
cd "$(dirname "$0")/.."
T=$(grep '^EXECUTOR_TOKEN=' .env.local | cut -d= -f2-)
DR=false; [ "$DRY" = "--dry-run" ] && DR=true
curl -s -m 300 -H "Authorization: Bearer $T" -H "Content-Type: application/json" \
  -d "{\"client\":\"$CLIENT\",\"tx_hash\":\"$TX\",\"dry_run\":$DR}" http://127.0.0.1:8743/cow/resubmit | python3 -c '
import json,sys
d=json.load(sys.stdin); d.pop("trace",None)
if d.get("error"): print("ERROR:", d["error"]); sys.exit(1)
for r in d.get("results",[]):
    o=r["order"]
    print(f"order: sell {o[\"sell_amount\"]/1e18:.6f} of {o[\"sell_token\"][:10]}… for at least {o[\"buy_amount\"]} raw of {o[\"buy_token\"][:10]}…, valid_to {o[\"valid_to\"]}")
    if r.get("dry_run"): print("dry run only; pre-signed on-chain:", r["presigned_uids"]); continue
    print("submitted UID:", r["uid"]); print("matches the pre-signature on-chain:", r["matches_presignature"]); print(r["url"])
'
