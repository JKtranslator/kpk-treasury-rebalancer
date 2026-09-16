#!/usr/bin/env bash
# Re-place a CoW order that a Safe transaction has already pre-signed on-chain but that never reached the CoW API.
# Usage (on the box): bash ~/kpk-treasury-rebalancer/deploy/resubmit.sh <client> <tx_hash> [dry]
set -euo pipefail
CLIENT="${1:?client slug (ens|nexus|cow|balancer)}"; TX="${2:?transaction hash}"; DRY="${3:-}"
cd "$(dirname "$0")/.."
T=$(grep '^EXECUTOR_TOKEN=' .env.local | cut -d= -f2-)
DR=false; [ "$DRY" = "dry" ] && DR=true
BODY=$(python3 -c "import json,sys; print(json.dumps(dict(client=sys.argv[1], tx_hash=sys.argv[2], dry_run=sys.argv[3]=='true')))" "$CLIENT" "$TX" "$DR")
curl -s -m 300 -H "Authorization: Bearer $T" -H "Content-Type: application/json" -d "$BODY" http://127.0.0.1:8743/cow/resubmit > /tmp/resubmit.json
python3 - <<'PY'
import json
d = json.load(open("/tmp/resubmit.json")); d.pop("trace", None)
if d.get("error"):
    print("ERROR:", d["error"]); raise SystemExit(1)
for r in d.get("results", []):
    o = r["order"]
    print("order: sell", o["sell_amount"] / 1e18, "of", o["sell_token"][:10] + "...", "for at least", o["buy_amount"], "raw of", o["buy_token"][:10] + "...", "valid_to", o["valid_to"])
    if r.get("dry_run"):
        print("dry run only; pre-signed on-chain:", r["presigned_uids"]); continue
    print("submitted UID:", r["uid"])
    print("matches the pre-signature on-chain:", r["matches_presignature"])
    print(r["url"])
PY
