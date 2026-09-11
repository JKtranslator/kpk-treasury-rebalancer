"""Per-client worker that runs inside a SafeAgent client folder (the kpk proposer bot's code).

    python executor_worker.py --client-dir <SafeAgentAll/ens> plan    < JSON {"commands": [...]}
    python executor_worker.py --client-dir <SafeAgentAll/ens> propose < JSON {"plan_file": "..."}

Same runtime setup as shared/core/client_worker.py::_load_client_runtime (client dir + shared/core
on sys.path, cwd = client dir, root .env then client .env). Uses the bot's own modules:
  intent_parser.parse_command      -> intent dict  (same grammar as Telegram)
  proposal_planner._resolve_all_amount / build_steps   -> raw Roles steps (permission-gated)
  propose_tx._wrap_with_role + batch_builder.build_multisend -> manager tx
  simulation_engine.simulate       -> Tenderly bundle simulation
  propose_tx.propose_manager_tx    -> Safe Transaction Service proposal (the TG "approved" step)

`plan` never proposes. `propose` only ever sends a manager_tx that a previous `plan` produced.
Output: one JSON object on stdout. Secrets are never printed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import sys
import traceback
import uuid
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


def load_runtime(client_dir: Path):
    root = client_dir.parent
    shared_core = root / "shared" / "core"
    sys.path[:] = [str(client_dir), str(shared_core)] + [p for p in sys.path if "SafeAgentAll" not in p]
    os.chdir(client_dir)
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=root / ".env", override=False)
    load_dotenv(dotenv_path=client_dir / ".env", override=True)


def out(obj):
    print(json.dumps(obj, default=str))
    sys.exit(0)


def fail(msg, **extra):
    out(dict(error=msg, **extra))


def tenderly_links(sim: dict) -> dict:
    """The bot builds a /public/ link after a share POST whose failure it swallows. Check the share
    result ourselves and always return the private dashboard URL too."""
    import re
    import requests
    import simulation_engine as se
    links = list(sim.get("links") or ([sim["link"]] if sim.get("link") else []))
    out_links, share_note = [], None
    for link in links:
        m = re.search(r"/simulator/([0-9a-f-]{20,})", link)
        if not m:
            out_links.append(dict(public=link))
            continue
        sim_id = m.group(1)
        private = f"https://dashboard.tenderly.co/{se.TENDERLY_USER}/{se.TENDERLY_PROJECT}/simulator/{sim_id}"
        status = None
        try:
            r = requests.post(f"{se._BASE}/simulations/{sim_id}/share", headers=se._HEADERS, timeout=10)
            status = r.status_code
            if r.status_code >= 400 and not share_note:
                body = r.text[:200].replace("\n", " ")
                share_note = (f"Tenderly refused to make the simulation public (HTTP {r.status_code}: {body}). "
                              "Public simulator links need a plan that allows sharing; use the private link (Tenderly login).")
        except Exception as e:
            share_note = share_note or f"share call failed: {e}"
        out_links.append(dict(id=sim_id, public=link, private=private, share_status=status))
    return dict(links=out_links, share_note=share_note)


def do_plan(client_dir: Path, payload: dict):
    load_runtime(client_dir)
    from intent_parser import parse_command
    import proposal_planner as pp
    from propose_tx import _wrap_with_role
    from batch_builder import build_multisend
    from simulation_engine import simulate
    import config

    commands = payload.get("commands") or []
    if not commands:
        fail("no commands")
    intents, all_steps, labels = [], [], []
    for cmd in commands:
        intent = parse_command(cmd)
        if intent.get("error"):
            fail(f"parser rejected `{cmd}`: {intent['error']}", command=cmd)
        if intent.get("type") != "execute":
            fail(f"`{cmd}` is not an executable command ({intent.get('type')})", command=cmd)
        try:
            pp._resolve_all_amount(intent)
            steps, extra = pp.build_steps(intent)
        except Exception as e:
            fail(f"build failed for `{cmd}`: {e}", command=cmd, trace=traceback.format_exc(limit=3))
        if extra.get("_cow_submit"):
            fail("swaps are not executed from the page; use the proposer bot for CoW orders", command=cmd)
        for s in steps:
            labels.append(f"{intent['protocol']} {intent['action']}" + (f" {intent.get('amount_human')}" if intent.get("amount_human") else ""))
        intents.append(intent)
        all_steps.extend(steps)
    if not all_steps:
        fail("no steps built")
    manager_steps = [_wrap_with_role(s) for s in all_steps]
    manager_tx = build_multisend(manager_steps)
    # The bot's pre-flight balance check reads current balances per intent. In a chained plan the
    # deposit leg is funded by the withdraw leg inside the same bundle, so the check would reject it
    # before Tenderly runs; simulate-bundle carries state between legs, so skip the pre-check there.
    chained = len(intents) > 1 and any(i.get("action") in ("withdraw", "redeem", "unwrap") for i in intents[:-1])
    skip = chained or any(i.get("wrap_eth") for i in intents)
    sim = simulate(manager_tx, all_steps=all_steps, skip_balance_check=skip)
    if chained:
        sim["note"] = "pre-flight balance check skipped for chained legs; Tenderly bundle result is authoritative"
    tl = tenderly_links(sim) if (sim.get("link") or sim.get("links")) else dict(links=[], share_note=None)

    plan_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    plans_dir = Path(payload.get("plans_dir") or (Path(__file__).resolve().parent.parent / "runs" / "plans"))
    plans_dir.mkdir(parents=True, exist_ok=True)
    plan_file = plans_dir / f"{plan_id}.json"
    record = dict(plan_id=plan_id, client_dir=str(client_dir), created=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                  commands=commands, intents=intents, steps=all_steps, manager_tx=manager_tx, simulation=sim, tenderly=tl,
                  safe=config.SAFE_ADDRESS, manager_safe=config.MANAGER_SAFE, roles_modifier=config.ROLES_MODIFIER, chain_id=config.CHAIN_ID)
    plan_file.write_text(json.dumps(record, default=str, indent=1), encoding="utf-8")

    SELECTORS = {"0x095ea7b3": "approve", "0x6e553f65": "deposit(assets,receiver)", "0xb460af94": "withdraw(assets,receiver,owner)",
                 "0xba087652": "redeem", "0x617ba037": "aave supply", "0x69328dec": "aave withdraw", "0xf9609f08": "stakewise deposit",
                 "0xa0712d68": "mint", "0xd0e30db0": "wrap ETH", "0x2e1a7d4d": "unwrap WETH", "0xf2b9fdb8": "compound supply",
                 "0xf3fef3a3": "compound withdraw", "0xa415bcad": "transfer"}

    def preview(s, label):
        data = s.get("data") or "0x"
        fn = SELECTORS.get(data[:10].lower())
        label = f"{label} · {fn}" if fn else label
        return dict(label=label, to=s.get("to"), value=str(s.get("value", 0)), operation=s.get("operation", 0),
                    selector=data[:10], data_preview=data[:74] + ("…" if len(data) > 74 else ""), data_len=len(data))
    out(dict(
        plan_id=plan_id, plan_file=str(plan_file), client_dir=client_dir.name,
        summary=f"{len(all_steps)} step(s) wrapped in execTransactionWithRole, bundled via MultiSend; from manager Safe {config.MANAGER_SAFE} to avatar {config.SAFE_ADDRESS}",
        commands=commands,
        transactions=[preview(s, labels[i] if i < len(labels) else "") for i, s in enumerate(all_steps)],
        manager_tx=dict(to=manager_tx.get("to"), value=str(manager_tx.get("value", 0)), operation=manager_tx.get("operation"), data_len=len(manager_tx.get("data") or "")),
        permission_check="every target/selector passed permission_engine.is_allowed (build would have raised otherwise)",
        simulation=dict(success=bool(sim.get("success")), error=sim.get("error"), url=(tl["links"][0].get("private") if tl["links"] else sim.get("link")),
                        public_url=sim.get("link"), links=tl["links"], share_note=tl["share_note"], needs_wrap=sim.get("needs_wrap"),
                        note=sim.get("note")),
        can_propose=bool(os.environ.get("AGENT_PRIVATE_KEY")) and bool(sim.get("success")),
    ))


def do_propose(client_dir: Path, payload: dict):
    load_runtime(client_dir)
    from propose_tx import propose_manager_tx
    pf = Path(payload.get("plan_file") or "")
    if not pf.exists():
        fail("plan file not found")
    rec = json.loads(pf.read_text(encoding="utf-8"))
    if not rec.get("simulation", {}).get("success"):
        fail("refusing to propose: the stored plan's simulation did not succeed")
    if not os.environ.get("AGENT_PRIVATE_KEY"):
        fail("AGENT_PRIVATE_KEY is not configured for this client; cannot propose")
    try:
        tx = propose_manager_tx(rec["manager_tx"])
    except Exception as e:
        fail(f"proposal failed: {e}", trace=traceback.format_exc(limit=3))
    rec["proposed"] = dict(at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), tx=tx)
    pf.write_text(json.dumps(rec, default=str, indent=1), encoding="utf-8")
    out(dict(plan_id=rec["plan_id"], safe_tx_hash=tx.get("hash"), url=tx.get("url"), nonce=tx.get("nonce"), safe=tx.get("safe")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client-dir", required=True)
    ap.add_argument("op", choices=["plan", "propose"])
    a = ap.parse_args()
    payload = json.loads(sys.stdin.read() or "{}")
    try:
        (do_plan if a.op == "plan" else do_propose)(Path(a.client_dir).resolve(), payload)
    except SystemExit:
        raise
    except Exception as e:
        fail(f"{type(e).__name__}: {e}", trace=traceback.format_exc(limit=4))


if __name__ == "__main__":
    main()
