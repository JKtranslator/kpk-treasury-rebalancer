# Output Format

The default recommendation is a tight, forwardable doc. Keep prose active and lead with the
decision; the reader has the context. Do not narrate the analysis trail.

## Template

```
Rebalancing Recommendations - <Date> — <Treasury>

Summary: <True NAV>, <asset A %> / <asset B %> (vs target). <Key state: flows done/pending,
caps, runway floor, idle assets>.

Expected impact: <blended APY before → after, the point change, $ /yr> (include only if there
is a yield change worth quoting).

Actions <Asset A>
* <WITHDRAW / DEPOSIT / DEPLOY / STAKE> <amount> <from/into venue> (<reason / yield>)
* ...

Actions <Asset B> (optional, yield only)
* ...

Rationale:
* <why each block, in one line each>

Note: <data caveats — incomplete vault data, reward-wrapper nuance, excluded venues, stale
sources — so a forwarded number doesn't get challenged>
```

## Rules

- **Every Action bullet is a real action.** If something stays put, it is not an action;
  drop it, or reframe it as the `DEPLOY/STAKE` that activates an idle balance. ("HOLD X" is
  not an action.)
- **Use the denomination the user wants.** Stables in USD; volatile assets often in native
  token units (e.g. ETH) with the price assumption stated. Confirm in intake.
- **Sizing figures stay tight.** Amounts sized to a cap or a target shouldn't be rounded into
  a breach. Round only where there's slack.
- **Two action types, kept separate:** parameter rebalance (bring within policy) vs
  performance rebalance (lift yield). Label optional/yield-only blocks as such.
- **Show the landing state** in the rationale or summary: resulting split, cap checks, floor.

## Rules for the Summary line when a floor exists

State the floor status and the **effective** stable target in the Summary, every time.
"On target" means on the *effective* target (`max(policy stable %, floor ÷ NAV)`), not the
nominal split. If the floor binds, say so in the first sentence — it's the fact the reader
will be challenged on.

## Worked example (ENS, IPS 2026 — floor-driven parameter rebalance)

Illustrative figures; the structure is the point. Under the 2026 IPS the ~$49.34M floor
binds at any NAV below ~$123M, so the ENS Summary line leads with it.

```
Rebalancing Recommendations - <Date> — ENS

Summary: True NAV ~$93.0M, ETH 66% / stable 34% ($31.6M). Stablecoin floor $49.34M is
BREACHED by ~$17.7M; effective stable target is 53% (floor ÷ NAV), not 40%. Protocol
concentration and sub-caps in-bounds. No idle assets.

Expected impact: none quoted — this is a mandate rebalance, not a yield trade.

Actions ETH (sell toward the floor; figures at ~$3,500/ETH)
* WITHDRAW 3,000 ETH from <lowest-yield LST venue> and SWAP to USDS (emergency tranche,
  IPS allows up to 3,000 ETH off-cycle when the floor is breached)
* WITHDRAW 1,500 ETH from <next venue> and SWAP to USDS at the next bi-weekly cycle
* (Repeat 1,500 ETH bi-weekly until stables ≥ $49.34M; ~5,060 ETH total at this price)

Actions Stablecoins
* DEPOSIT proceeds into <blue-chip stable venues>, spread to keep every protocol < 30%

Rationale:
* IPS: "Runway priority — Unconditional. Supersedes the 60/40 target in all circumstances."
  The like-for-like-rotation guardrail doesn't apply below the floor.
* Source the sales from the lowest-yield, policy-neutral ETH positions first.
* Landing state: ~$49.5M stables (53%), ETH 47%, all caps in-bounds, floor restored with a
  thin buffer; incoming protocol revenue rebuilds the buffer over the following months.

Note: Floor is the 2025-opex figure from the 2026 IPS; it resets on prior-year actuals at
the annual review. ETH price assumption stated above; the ETH quantity scales with it.
```

## Worked example (ENS, 2025-IPS era — idle deploy; kept for the reconciliation lesson)

This is the June 2026 assessment written against the **2025** IPS (25% cap, ~$26.7M floor).
Under the 2026 floor the same book is ~$22M short and the headline action changes to the
floor-driven sale above. Read it for the idle-asset reconciliation, not the numbers.

```
Rebalancing Recommendations - June 29th 2026 — ENS   [2025 IPS parameters]

Summary: True NAV ~$68.7M, ETH 60.9% / stable 39.1% (on target). $8.17M sweep complete;
stables $26.86M at the (2025) runway floor. Both staking protocols under the (2025) 25% cap.
~5,820 ETH (~$9.2M, weETH + raw ETH) sitting idle.

Expected impact: Deploying the idle ETH lifts blended yield 2.62% → ~2.94%, +0.32 pts,
~$218K/yr.

Actions ETH (deploy ~5,820 idle ETH; figures at ~$1,580/ETH)
* DEPLOY/STAKE 380 ETH into StakeWise (top to cap, 2.61%)
* DEPLOY/STAKE 710 ETH into Stader (top to cap, 2.40%)
* DEPLOY/STAKE 1,580 ETH into Rocket Pool (re-enter, 1.9%, decentralization)
* DEPLOY/STAKE 3,150 ETH (weETH) into ether.fi vault (2.40%)

Actions Stablecoins (optional, yield only)
* Consider rotating laggards into kpk USDC Yield V2 (6.28%)

Rationale:
* Within the then-current IPS parameters; no cap trim or ETH sale required.
* Idle ETH is the only drag; deploying it is a pure performance gain.
* Cap headroom forces the deploy to spread beyond StakeWise/Stader; Rocket Pool re-entry
  restores the decentralization preference.

Note: ~$218K/yr assumes the idle weETH is truly at 0%; it normally accrues ether.fi yield via
its wrapper, so confirm before quoting.
```

## Worked example (Nexus, pure yield, no policy)

```
Rebalancing Recommendations - <Date> — Nexus Mutual

Summary: Tracked vault book ~$X, blended yield ~Y%. No IPS; goal is pure yield.

Actions Stablecoins
* WITHDRAW $A from <laggard venue> (<low APY>)
* DEPOSIT $A into <higher venue> (<APY>)

Rationale:
* <yield pickup $/yr, risk note>

Note: <excluded venues e.g. a hacked protocol; tracked-only book caveat; not investment
advice>
```
