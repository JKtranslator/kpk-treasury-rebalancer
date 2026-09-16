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
    # prepend the client and shared/core dirs; keep everything else (the venv site-packages may live
    # under the SafeAgent folder itself, so never filter by folder name)
    keep = [p for p in sys.path if Path(p or ".").resolve() not in (client_dir, shared_core)]
    sys.path[:] = [str(client_dir), str(shared_core)] + keep
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
    swap_quote = None
    cow_submit = None
    cow_submits: list = []
    for cmd in commands:
        if "__SWAP_OUT__" in cmd:
            if not swap_quote:
                fail("deposit leg depends on a swap output but no swap leg produced a quote", command=cmd)
            from token_registry import get_decimals
            dec = get_decimals(swap_quote["buy_token"])
            human = swap_quote["min_buy_amount"] / 10 ** dec
            cmd = cmd.replace("__SWAP_OUT__", f"{human:.{min(dec, 6)}f}")
        intent = parse_command(cmd)
        if intent.get("error"):
            fail(f"parser rejected `{cmd}`: {intent['error']}", command=cmd)
        if intent.get("type") != "execute":
            fail(f"`{cmd}` is not an executable command ({intent.get('type')})", command=cmd)
        # A sell of native ETH sized a few wei above the Safe's balance (float rounding of a "max" figure) makes
        # the bot's balance check report plain insufficiency instead of needs_wrap, so the auto-wrap never fires.
        if intent.get("protocol") == "cowswap" and intent.get("action") in ("swap", "limit", "twap") \
                and str(intent.get("token", "")).upper() == "ETH" and intent.get("amount") and not intent.get("amount_all"):
            try:
                from planner_utils import get_w3
                bal = get_w3().eth.get_balance(config.SAFE_ADDRESS)
                if int(intent["amount"]) > bal:
                    from decimal import Decimal
                    fail(f"`{cmd}` sells {Decimal(int(intent['amount'])) / 10**18} ETH but the Safe holds {Decimal(bal) / 10**18} ETH "
                         f"({int(intent['amount'])-bal} wei short). Use a rounded-down amount; the page's Max button floors to 6 decimals.",
                         command=cmd)
            except SystemExit:
                raise
            except Exception:
                pass
        try:
            resolve = getattr(pp, "_resolve_all_amount", None)   # some client planners resolve inside build_steps
            if resolve:
                resolve(intent)
            steps, extra = pp.build_steps(intent)
        except Exception as e:
            fail(f"build failed for `{cmd}`: {e}", command=cmd, trace=traceback.format_exc(limit=3))
        if extra.get("_cow_submit"):
            x = dict(extra["_cow_submit"])
            x.setdefault("sell_symbol", (intent.get("token") or "").upper()); x.setdefault("buy_symbol", (intent.get("buy_token") or "").upper())
            parts = cmd.split(); x.setdefault("sell_human", parts[2] if len(parts) > 2 else None); x.setdefault("command", cmd)
            extra["_cow_submit"] = x
            cow_submits.append(x)   # each submitted to the CoW API at propose time, like execute_plan
            cow_submit = cow_submits[0]
        if extra.get("swap_quote"):
            q = extra["swap_quote"]
            from token_registry import get_decimals
            sd, bd = get_decimals(q["sell_token"]), get_decimals(q["buy_token"])
            sell_h, out_h, min_h = q["sell_amount"] / 10 ** sd, q["quote_buy_amount"] / 10 ** bd, q["min_buy_amount"] / 10 ** bd
            swap_quote = dict(q, sell_human=sell_h, quote_out_human=out_h, min_out_human=min_h,
                              impact_pct=round((sell_h - out_h) / sell_h * 100, 3) if sell_h else None)
            MAX_IMPACT_PCT = float(os.environ.get("MAX_SWAP_IMPACT_PCT", "0.5"))
            if swap_quote["impact_pct"] is not None and swap_quote["impact_pct"] > MAX_IMPACT_PCT:
                fail(f"on-chain swap not viable at this size: Uniswap v3 quotes {sell_h:,.0f} {q['sell_token']} -> "
                     f"{out_h:,.0f} {q['buy_token']} ({swap_quote['impact_pct']:.2f}% price impact, limit {MAX_IMPACT_PCT}%). "
                     f"Use a CoW order for the swap (asynchronous, so it cannot be bundled with the deposit): "
                     f"stage 1 withdraw + CoW swap via the proposer bot, stage 2 deposit once the {q['buy_token']} arrives.",
                     command=cmd, swap_quote=swap_quote)
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
    chained = len(intents) > 1 and any(i.get("action") in ("withdraw", "redeem", "unwrap", "claim") for i in intents[:-1])
    skip = chained or any(i.get("wrap_eth") for i in intents)
    sim = simulate(manager_tx, all_steps=all_steps, skip_balance_check=skip)
    if chained:
        sim["note"] = "pre-flight balance check skipped for chained legs; Tenderly bundle result is authoritative"
    # The bot's Telegram flow asks "wrap ETH into WETH? yes" when a WETH deposit exceeds the WETH balance
    # but the Safe holds ETH. Answer yes the same way the bot does: set wrap_eth on the intent, which makes
    # build_steps prepend the WETH wrap for the shortfall, then rebuild and re-simulate.
    nw = sim.get("needs_wrap") if not sim.get("success") else None
    if nw and isinstance(nw, dict):
        # Same as ens/bot_service._execute on needs_wrap: mark the intent so build_steps prepends WETH.deposit()
        # for the shortfall, then rebuild. The consumer of the wrapped native is a cowswap sell of ETH or WETH
        # (the builder normalises ETH -> WETH) or a WETH deposit; attach the wrap to the first such intent so the
        # deposit() lands before the step that needs it. Fall back to the first intent.
        wtok = str(nw.get("token") or "WETH").upper()
        native = "XDAI" if wtok == "WXDAI" else "ETH"
        cands = [it for it in intents if str(it.get("token", "")).upper() in (wtok, native)] or intents[:1]
        it = cands[0]
        it["wrap_eth"] = True
        it["wrap_token"] = wtok
        if nw.get("shortfall_raw"):
            it["wrap_shortfall_raw"] = int(nw["shortfall_raw"])
        if nw.get("shortfall"):
            it["wrap_shortfall_human"] = nw["shortfall"]
        all_steps, labels = [], []
        cow_submits, cow_submit = [], None     # the rebuild re-quotes every CoW order: what the Safe signs is the NEW order
        for it, cmd_ in zip(intents, commands):
            try:
                steps, extra = pp.build_steps(it)
            except Exception as e:
                fail(f"rebuild with ETH wrap failed: {e}", trace=traceback.format_exc(limit=3))
            if extra.get("_cow_submit"):
                x = dict(extra["_cow_submit"])
                parts = str(cmd_).split()
                x.setdefault("sell_symbol", (it.get("token") or "").upper()); x.setdefault("buy_symbol", (it.get("buy_token") or "").upper())
                x.setdefault("sell_human", parts[2] if len(parts) > 2 else None); x.setdefault("command", cmd_)
                cow_submits.append(x); cow_submit = cow_submits[0]
            for _s in steps:
                labels.append(f"{it['protocol']} {it['action']}" + (f" {it.get('amount_human')}" if it.get("amount_human") else ""))
            all_steps.extend(steps)
        manager_steps = [_wrap_with_role(s) for s in all_steps]
        manager_tx = build_multisend(manager_steps)
        sim = simulate(manager_tx, all_steps=all_steps, skip_balance_check=True)
        sim["note"] = (f"wrapped {float(nw.get('shortfall', 0)):.6f} ETH into WETH inside the bundle (Safe had {nw.get('weth_have')} WETH, "
                       f"{nw.get('eth_have')} ETH); pre-flight skipped, Tenderly bundle result is authoritative")
    if not sim.get("success") and not sim.get("error") and sim.get("needs_wrap"):
        sim["error"] = f"needs ETH wrap: {sim['needs_wrap']}"
    tl = tenderly_links(sim) if (sim.get("link") or sim.get("links")) else dict(links=[], share_note=None)

    signed = signed_orders(all_steps)
    if signed or cow_submits:
        if len(signed) != len(cow_submits) or not all(any(orders_match(sg, sb) for sb in cow_submits) for sg in signed):
            fail("internal consistency check failed: the CoW order(s) the steps pre-sign do not match the order(s) queued for API "
                 f"submission ({len(signed)} signed vs {len(cow_submits)} queued). Refusing to write a plan that would leave an unsigned order.",
                 signed=[dict(sell_amount=s["sell_amount"], buy_amount=s["buy_amount"], valid_to=s["valid_to"]) for s in signed],
                 queued=[dict(sell_amount=s.get("sell_amount"), buy_amount=s.get("buy_amount"), valid_to=s.get("valid_to")) for s in cow_submits])
    plan_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    plans_dir = Path(payload.get("plans_dir") or (Path(__file__).resolve().parent.parent / "runs" / "plans"))
    plans_dir.mkdir(parents=True, exist_ok=True)
    plan_file = plans_dir / f"{plan_id}.json"
    record = dict(plan_id=plan_id, client_dir=str(client_dir), created=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                  commands=commands, intents=intents, steps=all_steps, manager_tx=manager_tx, simulation=sim, tenderly=tl, swap_quote=swap_quote,
                  cow_submit=cow_submit, cow_submits=cow_submits,
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
        plan_id=plan_id, plan_file=str(plan_file), client_dir=client_dir.name, swap_quote=swap_quote,
        cow_orders=[dict(sell_token=x.get("sell_token"), buy_token=x.get("buy_token"), sell_amount=str(x.get("sell_amount")),
                         buy_amount=str(x.get("buy_amount")), valid_to=x.get("valid_to"), slippage_bps=x.get("slippage_bps"),
                         sell_symbol=x.get("sell_symbol"), buy_symbol=x.get("buy_symbol"), sell_human=x.get("sell_human"), command=x.get("command")) for x in cow_submits],
        cow_order=(dict(sell_token=cow_submit.get("sell_token"), buy_token=cow_submit.get("buy_token"),
                        sell_amount=str(cow_submit.get("sell_amount")), buy_amount=str(cow_submit.get("buy_amount")),
                        valid_to=cow_submit.get("valid_to"), slippage_bps=cow_submit.get("slippage_bps"),
                        note="pre-signed CoW order: submitted to the CoW API when you propose; fills after the Safe executes the setPreSignature step")
                   if cow_submit else None),
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


def do_quote(client_dir: Path, payload: dict):
    """CoW quote + dynamic slippage for the swap panel, through the bot's cow_api (same as the planner)."""
    load_runtime(client_dir)
    import time as _t
    import config
    from token_registry import get_address, get_decimals, normalize
    from cow_api import get_quote, get_slippage_tolerance_info
    sell, buy, amount = payload["sell"].upper(), payload["buy"].upper(), str(payload["amount"])
    wrapped = "WXDAI" if config.CHAIN_ID == 100 else "WETH"; native = "XDAI" if config.CHAIN_ID == 100 else "ETH"
    if {sell, buy} == {native, wrapped}:
        # not a swap at all: WETH.deposit() / WETH.withdraw() under Roles, 1:1, no CoW order, no slippage, no fee
        act = "wrap" if sell == native else "unwrap"
        amt = float(amount)
        out(dict(sell=sell, buy=buy, sell_amount=amt, sell_amount_after_fee=amt, fee=0.0, buy_amount=amt, price=1.0,
                 slippage_bps=0, slippage_source="n/a (wrap)", slippage_info={}, min_receive=amt, valid_to=None, expiration=None,
                 command=f"cowswap {act} {amount} {sell}", is_wrap=True,
                 note=f"{'Wrapping' if act == 'wrap' else 'Unwrapping'} is a direct {wrapped} contract call inside the Roles bundle, not a CoW order"))
        return
    # CoW cannot sell native ETH: the bot wraps first and the order sells WETH (same rule as cowswap_builder)
    quote_sell, note = (("WETH", "ETH is wrapped to WETH inside the bundle; the CoW order sells WETH") if sell == "ETH" else (sell, None))
    raw = normalize(amount, quote_sell)
    sell_addr, buy_addr = get_address(quote_sell), get_address(buy)
    try:
        q = get_quote(sell_addr, buy_addr, raw, config.SAFE_ADDRESS, int(_t.time()) + 30 * 60)
    except Exception as e:
        body = ""
        try:
            body = (e.response.json().get("description") or e.response.text)[:200]   # requests.HTTPError carries CoW's reason
        except Exception:
            pass
        fail(f"CoW quote failed for {amount} {sell} -> {buy}: {e}" + (f" — {body}" if body else ""))
    quote = q.get("quote") or {}
    try:
        sl = get_slippage_tolerance_info(sell_addr, buy_addr, config.CHAIN_ID, sell_amount=raw)
    except Exception as e:
        sl = {"error": str(e)[:120]}
    bps = sl.get("slippage_bps") if isinstance(sl.get("slippage_bps"), int) else 50
    sd, bd = get_decimals(quote_sell), get_decimals(buy)
    buy_amt = int(quote.get("buyAmount") or 0)
    out(dict(sell=sell, buy=buy, sell_amount=raw / 10 ** sd, sell_amount_after_fee=int(quote.get("sellAmount") or raw) / 10 ** sd,
             fee=int(quote.get("feeAmount") or 0) / 10 ** sd, buy_amount=buy_amt / 10 ** bd,
             price=(buy_amt / 10 ** bd) / (raw / 10 ** sd) if raw else None, slippage_bps=bps, slippage_source=sl.get("source") or sl.get("mode"),
             slippage_info={k: v for k, v in sl.items() if k != "raw"}, min_receive=(buy_amt * (10_000 - bps) // 10_000) / 10 ** bd,
             valid_to=quote.get("validTo"), expiration=q.get("expiration"), command=f"cowswap swap {amount} {sell} {buy}", note=note))


SIGN_ORDER_SEL = "0x569d3489"   # CowswapOrderSigner.signOrder(GPv2Order.Data, uint32 validDuration, uint256 feeAmountBP)


def decode_sign_order(data: str) -> dict | None:
    """The GPv2Order.Data struct inside a signOrder call. This is exactly the order the Safe pre-signs on-chain."""
    d = (data or "").lower()
    i = d.find(SIGN_ORDER_SEL[2:])
    if i < 0:
        return None
    seg = d[i + 8:]
    w = lambda k: seg[k * 64:(k + 1) * 64]
    try:
        return dict(sell_token="0x" + w(0)[24:], buy_token="0x" + w(1)[24:], receiver="0x" + w(2)[24:],
                    sell_amount=int(w(3), 16), buy_amount=int(w(4), 16), valid_to=int(w(5), 16),
                    app_data="0x" + w(6), fee_amount=int(w(7), 16), partially_fillable=bool(int(w(9), 16)))
    except Exception:
        return None


def signed_orders(steps: list) -> list[dict]:
    out = []
    for s in steps or []:
        o = decode_sign_order(s.get("data") or "")
        if o:
            out.append(o)
    return out


def orders_match(signed: dict, sub: dict) -> bool:
    """Same order iff the fields that enter the order digest agree."""
    g = lambda k: str(sub.get(k) if sub.get(k) is not None else "").lower()
    return (g("sell_token") == signed["sell_token"] and g("buy_token") == signed["buy_token"]
            and int(sub.get("sell_amount") or 0) == signed["sell_amount"] and int(sub.get("buy_amount") or 0) == signed["buy_amount"]
            and int(sub.get("valid_to") or 0) == signed["valid_to"]
            and g("receiver") in ("", signed["receiver"]))


def do_tokens(client_dir: Path, payload: dict):
    """Symbols the client's bot token registry knows: the swap panel greys out everything else."""
    load_runtime(client_dir)
    from token_registry import TOKENS
    out(dict(tokens=sorted(TOKENS.keys()), addresses={k: v["address"].lower() for k, v in TOKENS.items()},
             decimals={k: v.get("decimals", 18) for k, v in TOKENS.items()}))


def do_resubmit(client_dir: Path, payload: dict):
    """Re-place a CoW order that a Safe transaction has already pre-signed on-chain but that never reached the API.
    Decodes the GPv2Order from the signOrder calldata of `tx_hash`, checks it is not expired and that the settlement
    contract emitted PreSignature for its owner, then submits it through the bot's cow_api. The UID the API returns
    must equal the one in the PreSignature event, otherwise the params differ and nothing useful was created."""
    load_runtime(client_dir)
    import time as _t
    import config
    from cow_api import submit_presign_order, order_url
    from planner_utils import get_w3
    w3 = get_w3()
    txh = payload.get("tx_hash")
    if not txh:
        fail("tx_hash required")
    tx = w3.eth.get_transaction(txh); rc = w3.eth.get_transaction_receipt(txh)
    if rc.status != 1:
        fail(f"{txh} did not succeed on-chain")
    orders = signed_orders([dict(data=tx["input"].hex() if hasattr(tx["input"], "hex") else tx["input"])])
    if not orders:
        fail("no signOrder call found in that transaction")
    settlement = "0x9008d19f58aabd9ed0d60971565aa8510560ab41"
    uids = []
    for lg in rc.logs:
        if lg.address.lower() == settlement:
            d = lg.data.hex() if hasattr(lg.data, "hex") else str(lg.data)
            d = d[2:] if d.startswith("0x") else d
            ln = int(d[128:192], 16); uids.append("0x" + d[192:192 + ln * 2])
    results = []
    for o in orders:
        if o["receiver"] != config.SAFE_ADDRESS.lower():
            fail(f"order receiver {o['receiver']} is not this client's Safe {config.SAFE_ADDRESS}")
        if o["valid_to"] < int(_t.time()):
            fail(f"order expired at {dt.datetime.fromtimestamp(o['valid_to'], dt.timezone.utc).isoformat()}; a new order is needed")
        if payload.get("dry_run"):
            results.append(dict(order=o, presigned_uids=uids, dry_run=True)); continue
        uid = submit_presign_order(sell_token=o["sell_token"], buy_token=o["buy_token"], sell_amount=o["sell_amount"],
                                   buy_amount=o["buy_amount"], receiver=config.SAFE_ADDRESS, valid_to=o["valid_to"],
                                   partially_fillable=o["partially_fillable"])
        results.append(dict(order=o, uid=uid, url=order_url(uid), presigned_uids=uids,
                            matches_presignature=uid.lower() in [u.lower() for u in uids]))
    out(dict(tx_hash=txh, results=results))


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
    # CoW leg, same order as execute_plan: submit the pre-signed order to the CoW API, then propose the
    # Safe tx that sets the presignature. If the API refuses, store the order for `cow resubmit`.
    cow_uid = cow_url = cow_warning = None
    cow_uids: list = []
    submits = rec.get("cow_submits") or ([rec["cow_submit"]] if rec.get("cow_submit") else [])
    cow_submit = submits[0] if submits else None
    signed = signed_orders(rec.get("steps") or [])
    if signed and (len(signed) != len(submits) or not all(any(orders_match(sg, sb) for sb in submits) for sg in signed)):
        fail("refusing to propose: the CoW order(s) this plan pre-signs differ from the order(s) it would submit to the API "
             "(a re-quote changed the order after the plan was built). Rebuild the plan.")
    if submits:
        from cow_api import submit_presign_order, order_url
        for sub in submits:
            try:
                uid = submit_presign_order(**sub)
                cow_uids.append(dict(uid=uid, url=order_url(uid), sell_token=sub.get("sell_token")))
            except Exception as e:
                cow_warning = f"CoW API submission failed for {sub.get('sell_token')} ({str(e)[:120]}); order stored for `cow resubmit <safe tx hash>` after execution"
        if cow_uids:
            cow_uid, cow_url = cow_uids[0]["uid"], cow_uids[0]["url"]
    try:
        tx = propose_manager_tx(rec["manager_tx"])
    except Exception as e:
        fail(f"proposal failed: {e}", trace=traceback.format_exc(limit=3), cow_order_uid=cow_uid)
    if cow_submit and not cow_uid:
        try:
            from cow_order_store import save as save_cow_order
            save_cow_order(tx["hash"], cow_submit)
        except Exception as e:
            cow_warning = f"{cow_warning}; storing the order also failed: {e}"
    rec["proposed"] = dict(at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), tx=tx, cow_order_uid=cow_uid, cow_url=cow_url)
    pf.write_text(json.dumps(rec, default=str, indent=1), encoding="utf-8")
    out(dict(plan_id=rec["plan_id"], safe_tx_hash=tx.get("hash"), url=tx.get("url"), nonce=tx.get("nonce"), safe=tx.get("safe"),
             cow_order_uid=cow_uid, cow_url=cow_url, cow_orders=cow_uids, cow_warning=cow_warning))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client-dir", required=True)
    ap.add_argument("op", choices=["plan", "propose", "quote", "tokens", "resubmit"])
    a = ap.parse_args()
    payload = json.loads(sys.stdin.read() or "{}")
    try:
        {"plan": do_plan, "propose": do_propose, "quote": do_quote, "tokens": do_tokens, "resubmit": do_resubmit}[a.op](Path(a.client_dir).resolve(), payload)
    except SystemExit:
        raise
    except Exception as e:
        fail(f"{type(e).__name__}: {e}", trace=traceback.format_exc(limit=4))


if __name__ == "__main__":
    main()
