"""One-shot runner: holdings -> yields -> assessment for a kpk client.

    python run.py --client ens [--out <dir>] [--period 7day] [--chain 1] [--exclude gearbox]
                  [--min-pickup-bps 50] [--min-move-usd 250000] [--venue-tvl-cap-pct 10]

Default output dir: <skill>/runs/<client>/<YYYY-MM-DD>/ . Prints the path of assessment.md.
Exit code 2 means the NAV reconciliation did not tie: read the flags before doing anything else.
"""
from __future__ import annotations

import argparse
import datetime as dt
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--period", default="7day")
    ap.add_argument("--chain", type=int, default=None)
    ap.add_argument("--exclude", nargs="*", default=[])
    ap.add_argument("--min-pickup-bps", default="50")
    ap.add_argument("--min-move-usd", default="250000")
    ap.add_argument("--venue-tvl-cap-pct", default="20")
    ap.add_argument("--skip-yields", action="store_true", help="client without a Strategy API entry")
    a = ap.parse_args()
    out = Path(a.out) if a.out else HERE.parent / "runs" / a.client / dt.date.today().isoformat()
    out.mkdir(parents=True, exist_ok=True)
    py = sys.executable
    chain = ["--chain", str(a.chain)] if a.chain else []

    rc = subprocess.call([py, str(HERE / "fetch_holdings.py"), "--client", a.client, "--out", str(out), "--period", a.period, *chain])
    if rc not in (0, 2):
        sys.exit(rc)
    if not a.skip_yields:
        rc2 = subprocess.call([py, str(HERE / "fetch_yields.py"), "--client", a.client, "--out", str(out), "--period", a.period, *chain])
        if rc2:
            print("yields step failed; assessment will run on holdings only if yields.json exists")
    else:
        (out / "yields.json").write_text('{"permitted": [], "vault_data_fetched_at": null}', encoding="utf-8")
    rc3 = subprocess.call([py, str(HERE / "assess.py"), "--client", a.client, "--out", str(out),
                           "--min-pickup-bps", a.min_pickup_bps, "--min-move-usd", a.min_move_usd,
                           "--venue-tvl-cap-pct", a.venue_tvl_cap_pct, *(["--exclude", *a.exclude] if a.exclude else [])])
    if rc3:
        sys.exit(rc3)
    print(f"\nASSESSMENT: {out / 'assessment.md'}")
    if rc == 2:
        print("NAV RECONCILIATION DID NOT TIE (exit 2). Resolve the flags in holdings.json before recommending anything.")
    sys.exit(rc)


if __name__ == "__main__":
    main()
