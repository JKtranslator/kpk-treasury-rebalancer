# Policy Checks

> kpk fork: the machine-checkable parameters live in `clients.json` → `clients.<slug>.policy` and are
> applied by `scripts/assess.py` (floor, effective stable target, single-protocol cap, whitelists).
> Everything below that needs judgement (sleeves, sub-caps, LSD share, regime) is yours to apply.

How to evaluate a treasury against an investment policy (IPS / mandate). If there's no
policy, skip this and run the performance assessment (Step 4 of SKILL.md) as pure yield
optimization.

Read the actual policy the user provides; the constraints below are the common ones, not a
fixed list. Capture the policy's specific numbers during intake. **For the ENS Endowment,
do not rely on the numbers in this file — load the `ens-ips-2026` skill**, which carries the
verbatim IPS text; the ENS section at the bottom is a summary that can go stale.

## The common constraints

### Liquidity / runway floor — check this FIRST
A minimum stable reserve, often expressed as N years of operating expenses. In most
mandates the floor is **unconditional and outranks the target allocation**, so compute it
before the split, not after:

```
effective stable target (%) = max( policy stable %,  floor ÷ NAV )
```

If `floor ÷ NAV` is above the policy's stable percentage, the floor is the binding
constraint and the nominal split (e.g. 60/40) is unreachable without a breach. State this
plainly in the Summary line — it changes what "on target" means. Flag any move that would
breach the floor or land right on it (no buffer). Where the floor is thin, the fix is
usually incoming revenue first; but if the policy authorises emergency rebalancing when the
floor is at risk, selling the growth asset is the mandated action, not a judgment call.

### Target allocation
A split between a volatile growth asset and stables (e.g. 60% ETH / 40% stablecoin), by
market value, with LSTs and staked positions counted in the growth bucket. Measure the drift
from the *effective* target above. A few points off is usually within tolerance; large
drift, or a scheduled flow that will move it, is the trigger to act. The target is a
strategic mandate, not a yield call — current yields may favor stables while the policy still
wants ETH for growth.

### Per-protocol concentration cap
A maximum share of NAV in any single protocol (e.g. 30%). This is a *per-protocol* limit, not
per-token. Two things to get right:
- Compute each protocol's share **of current NAV** (including reconciled idle assets).
- Caps recompute after flows. **An outflow shrinks NAV and can push a static position over
  the cap with no trade at all.** Always re-check caps on the post-flow NAV.

When deploying into the book, respect the cap too: if the highest-yield venue is near its
cap, it only has limited headroom, so spread the deployment.

### Risk sleeves
Many policies split the book into a low-risk sleeve with a minimum (e.g. ≥90%) and a
moderate-risk sleeve with a maximum (e.g. ≤10%), each with its own definition (track record,
audit, redemption window). A new venue has to be classified before it's sized: a
higher-yield vault that fails the low-risk test consumes the moderate sleeve, and that
sleeve may already be full.

### Sub-caps
Beyond the single-protocol cap, watch for caps on a *category*: RWAs, permissioned or
KYC-gated venues, non-USD stablecoins, any one LSD's share of consensus. Each is computed
against its own base (total NAV, stablecoin allocation, or the protocol's consensus share),
so read the denominator in the policy text.

### Whitelists and preferences
- **Network** (e.g. mainnet only), **assets** (e.g. ETH + named stables), **strategies**
  (lending, same-denominator LP, staking, delta-neutral cash-and-carry).
- **Decentralization / LSD preferences**: many policies favor decentralized, permissionless
  staking and cap exposure to any LSD that nears a consensus threshold. This can conflict
  with pure yield (the most decentralized option may yield least). Surface the trade rather
  than optimizing yield blindly.

### Operational funding vs portfolio rebalancing
Some mandates run two separate processes: a periodic operational top-up to a spending wallet
(no proposal needed) and the portfolio rebalance. Don't net one against the other — a top-up
is an outflow that shrinks NAV and re-triggers every cap and floor check above.

## Reporting each check

For each constraint, state: in-bounds / near the line / breached, with the number. Separate
**hard breaches** (cap, floor, whitelist, sleeve) from **soft preferences** (decentralization
tilt). A recommendation should clear every hard breach and consciously decide on soft ones.

## Worked instance: ENS Endowment — IPS 2026 (EP 6.46)

Current parameters, from the 2026 IPS. **Authoritative text and section references are in the
`ens-ips-2026` skill; re-verify there before quoting.** Oversight body is the ENS Foundation
(the IPS text says Meta-Governance WG — an accepted deviation since Aug 2026).

| Constraint | Value | Note |
|---|---|---|
| Stablecoin floor | **~$49.34M** at the Endowment level (3 × 2025 opex of $16.4M) | Unconditional; supersedes 60/40 in all circumstances; re-set annually on prior-year actuals |
| Effective stable target | `max(40%, $49.34M ÷ NAV)` | At NAV ≈ $93M this is ~53% stables. 60/40 is only reachable above NAV ≈ $123M |
| Timelock buffer | Separate 6-month USDC buffer in wallet.ensdao.eth, KPK tops up quarterly | Does **not** count toward the Endowment floor; the top-up is an outflow from the Endowment |
| Target allocation | 60% ETH + LSTs / 40% stables, by market value | Subject to the floor above |
| Rebalancing | Bi-weekly, **1,500 ETH** per event; **3,000 ETH** off-cycle if the floor is breached or at material risk | |
| Single-protocol cap | **30%** of Endowment | Reported monthly |
| Risk sleeves | ≥90% low-risk / ≤10% moderate-risk | Low-risk: >12-mo track record, audited, redeemable ≤7 days, no novel mechanisms. Moderate: reversible ≤30 days, disclosed monthly |
| RWAs | ≤5% of Endowment; tokenised MMFs / govt bonds only; ≤7 business-day redemption | Separate line item per position |
| Permissioned providers | ≤10% of Endowment | |
| EURC | ≤5% of the stablecoin allocation | Only permitted non-USD stable |
| LSD consensus | No LSD >20% of validator consensus; DVT exception up to 10% of portfolio at ≤25% share | Prefer permissionless node-operator LSTs among those under the threshold |
| CEX | Never for ETH | |
| Network | Ethereum mainnet only | |
| Withdrawals | Stables first; each withdrawal ≥6 months projected opex (~$8.2M at H1 2026 run-rate); any floor breach needs a DAO vote; restore 60/40 after | |
| Allowed stables | USDC, USDT, DAI (legacy), USDS (primary), GHO, EURC | |
| Allowed ETH/LSTs | ETH/WETH, stETH, rETH, eETH, osETH, ETHx, OETH + wrapped | |

Things in this skill's earlier guidance that the 2026 IPS changed: the cap was 25% (now
30%), the floor was ~$26.7M (now ~$49.34M), tranches were 1,000 ETH (now 1,500 / 3,000
emergency), and RWAs were not permitted (now ≤5%). If a reader cites the old numbers,
they're on the 2025 IPS.

**How the "don't sell ETH into weakness" guardrail interacts with this policy.** The
preference for like-for-like LST rotation over market sales is a KPK operating practice, not
IPS text. It applies to cap fixes and to allocation drift *above* the floor. It does not
apply when the stablecoin floor is breached or at material risk — there the IPS authorises
and expects off-cycle ETH sales up to 3,000 ETH per tranche. State which regime you're in.

### Worked moment (June 2026, on the 2025 floor — kept for the reasoning, not the numbers)

A scheduled stablecoin outflow shrank NAV, which (a) lifted ETH over target and (b) pushed a
staking provider over the then-25% cap purely from the NAV drop. Because idle weETH from an
earlier exit was sitting undeployed, true NAV was higher than the tracked vault total — once
reconciled, the book was actually on target and under the cap, and the real action was
simply deploying the idle ETH (spread across providers to respect the cap), not selling
anything. The lesson — reconcile idle assets before concluding anything is breached — is
unchanged. Under the 2026 floor the same book would have been ~$22M short of the floor, and
the headline action would have been a floor-driven ETH sale, not an idle deploy.
