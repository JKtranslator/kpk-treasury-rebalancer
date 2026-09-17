"""Local executor for the page's Execute button, on top of the kpk proposer bot (SafeAgent).

    python executor.py [--port 8743] [--safeagent <path to Codex/SafeAgentAll>]

Runs on 127.0.0.1 only. The page (local or GitHub Pages) calls:
  GET  /health          -> {ok, clients: [...], propose: {client: bool}}
  POST /plan            -> body = the move the simulator produced; returns the built Roles
                           transaction(s), permission result and Tenderly simulation. Nothing sent.
  POST /propose         -> {plan_id} ; proposes the stored manager tx to the Safe Transaction
                           Service, exactly what the Telegram "approved" step does.
Each request runs executor_worker.py as a subprocess inside the client's SafeAgent folder, so
the client's .env, parser, permission engine and builders are the bot's own. This process never
loads a private key itself.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import os
from common import client, fnum, load_env, registry

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
WORKSPACE = ROOT.parent.parent  # .../Karpatkey (ROOT is flows/rebalancer)
SAFEAGENT = WORKSPACE / "Codex" / "SafeAgentAll"
PLANS = ROOT / "runs" / "plans"

# rebalancer client slug -> SafeAgent client folder
CLIENT_DIRS = {"ens": "ens", "nexus": "nexus", "cow": "cow dao", "balancer": "balancer"}
KNOWN_TOKENS: dict[str, list] = {}   # slug -> symbols in the bot's token registry (per process cache)
KNOWN_ADDR: dict[str, dict] = {}     # slug -> {symbol: address} from the same registry
NATIVE = "0x0000000000000000000000000000000000000000"


def known_tokens(slug: str) -> tuple[list, dict]:
    if slug not in KNOWN_TOKENS:
        res = run_worker(slug, "tokens", {})
        if res.get("tokens"):
            KNOWN_TOKENS[slug] = res["tokens"]
            KNOWN_ADDR[slug] = res.get("addresses") or {}
    return KNOWN_TOKENS.get(slug, []), KNOWN_ADDR.get(slug, {})

# Strategy API protocol key -> bot protocol word
PROTO = {"morphoVaults": "morpho", "aave_v3": "aave", "compound_v3": "compound", "fluid": "fluid", "sky": "sky",
         "spark": "spark", "stakewise_v3": "stakewise", "lido": "lido", "ether_fi": "etherfi", "stader": "stader",
         "rocket_pool": "rocketpool", "gearbox": "gearbox"}
STABLES = {"USDC", "USDT", "USDS", "DAI", "GHO", "EURC", "PYUSD", "RLUSD"}
ALLOWED_ORIGINS = ("http://localhost:", "http://127.0.0.1:", "https://jktranslator.github.io", "https://82-70-94-93.sslip.io")
PROTECTED = ("/plan", "/propose", "/refresh/", "/live/")   # need Authorization: Bearer <EXECUTOR_TOKEN>
LIVE_CACHE: dict[str, tuple[float, dict]] = {}   # slug -> (built_at, snapshot); the fast lane is cheap but not free
LIVE_LOCK = threading.Lock()
LIVE_TTL = 600.0        # a DeBank rebuild costs ~36 units; a view is reused for 10 min unless a Safe tx executed (safe-tx poll drops it)
LIVE_FORCE_MIN = 60.0   # the Refresh button forces a rebuild, but never more than once a minute


def live_view(slug: str, force: bool = False) -> dict:
    """Snapshot-shaped live view from DeBank + Safe + vaults.fyi (scripts/live_lane.py), built in-process, cached LIVE_TTL."""
    now = time.time()
    with LIVE_LOCK:
        hit = LIVE_CACHE.get(slug)
        if hit and now - hit[0] < (LIVE_FORCE_MIN if force else LIVE_TTL):
            return dict(hit[1], live_cached=True, live_age_s=round(now - hit[0]))
    from live_lane import live_snapshot
    snap = live_snapshot(slug, load_snapshot(slug))
    with LIVE_LOCK:
        LIVE_CACHE[slug] = (time.time(), snap)
    return snap


def safe_tx_status(chain_id: int, safe_tx_hash: str) -> dict:
    """Has the Safe executed this proposal yet? (Safe Transaction Service, read-only.)"""
    svc = registry()["safe_tx_service"][str(chain_id)]
    k = os.environ.get("SAFE_API_KEY", "")
    url = f"{svc['gateway'] if k else svc['legacy']}/multisig-transactions/{safe_tx_hash}/"
    import urllib.request
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {k}"} if k else {})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode())
    return dict(safe_tx_hash=safe_tx_hash, is_executed=bool(d.get("isExecuted")), is_successful=d.get("isSuccessful"),
                tx_hash=d.get("transactionHash"), execution_date=d.get("executionDate"), nonce=d.get("nonce"),
                confirmations=len(d.get("confirmations") or []), required=d.get("confirmationsRequired"))


def safe_queue(chain_id: int, safe: str, slug: str) -> dict:
    """Everything awaiting signatures on the Safe, whoever proposed it. The page can only know about proposals it
    made itself; the bot's go straight to the Safe, and a queue you cannot see is a queue you sign blind."""
    reg = registry()
    svc = reg["safe_tx_service"][str(chain_id)]
    k = os.environ.get("SAFE_API_KEY", "")
    import urllib.request
    base = svc["gateway"] if k else svc["legacy"]
    hdr = {"Authorization": f"Bearer {k}"} if k else {}

    def get(url):
        req = urllib.request.Request(url, headers=hdr)
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())

    # proposals are raised on the manager Safe, which signs and calls the Roles modifier on the avatar; the avatar
    # itself is checked too, since an owner can always propose there directly
    c = client(slug)
    safes = []
    for role in ("manager", "avatar"):
        a = (c["safes"].get(str(chain_id)) or {}).get(role)
        if a and a.lower() not in [x[1].lower() for x in safes]:
            safes.append((role, a))
    if not safes:
        safes = [("avatar", safe)]
    # name what the transaction touches, so a bot proposal is not just an address
    names = {}
    snap = load_snapshot(slug) if slug in CLIENT_DIRS else {}
    for p in (snap.get("permitted") or []):
        if p.get("vault"):
            names[p["vault"].lower()] = f"{p['protocol']} {p['asset']}"
    for b in (snap.get("book") or []):
        for a in (b.get("vault"), b.get("receipt")):
            if a:
                names.setdefault(a.lower(), f"{b['protocol']} {b.get('symbol') or ''}".strip())
    for a, t in (reg.get("receipt_tokens") or {}).items():
        if isinstance(t, dict):        # the registry keeps a _comment string alongside the entries
            names.setdefault(a.lower(), t.get("position") or t.get("symbol") or "")
    roles = ((snap.get("safes") or {}).get("roles_mod") or "").lower()
    if roles:
        names.setdefault(roles, "Roles modifier")
    out, nonces = [], {}
    rows = []
    for role, addr in safes:
        info = get(f"{base}/safes/{addr}/")
        n = int(info.get("nonce") or 0)
        nonces[role] = dict(safe=addr, nonce=n, threshold=info.get("threshold"))
        q = get(f"{base}/safes/{addr}/multisig-transactions/?executed=false&nonce__gte={n}&ordering=nonce&limit=40")
        rows += [(role, addr, t, False) for t in q.get("results", [])]
    # a bot proposal arrives as multiSend(execTransactionWithRole, ...) through the Roles modifier, so the method the
    # Safe reports says nothing. The verb is the call data's selector and the venue is the Roles call's own target.
    VERBS = {"0x095ea7b3": "approve", "0x6e553f65": "deposit", "0xb6b55f25": "deposit", "0x617ba037": "supply",
             "0xf2b9fdb8": "supply", "0x2e1a7d4d": "withdraw", "0xf3fef3a3": "withdraw", "0x69328dec": "withdraw",
             "0xba087652": "redeem", "0xdb006a75": "redeem", "0x3ccfd60b": "withdraw", "0xd0e30db0": "wrap",
             "0x569d3489": "sign CoW order", "0xccc143b8": "request withdrawal", "0x441a3e70": "withdraw",
             "0x4782f779": "withdraw", "0xa9059cbb": "transfer", "0x1249c58b": "mint", "0xdd62ed3e": "allowance"}

    def legs(t):
        """(verb, target address) for every call this transaction really makes, unwrapping multiSend and Roles."""
        outl = []
        def walk(to, dec, data):
            m = (dec or {}).get("method")
            if m == "multiSend":
                for pr in (dec.get("parameters") or []):
                    for x in (pr.get("valueDecoded") or []):
                        walk(x.get("to"), x.get("dataDecoded"), x.get("data"))
                return
            if m == "execTransactionWithRole" or (m or "").startswith("execTransaction"):
                inner_to = inner_data = None
                for pr in (dec.get("parameters") or []):
                    if (pr.get("name") or "").lower() == "to":
                        inner_to = pr.get("value")
                    if (pr.get("name") or "").lower() == "data":
                        inner_data = pr.get("value")
                walk(inner_to, None, inner_data)
                return
            sel = (data or "")[:10].lower()
            outl.append((VERBS.get(sel) or m or sel or "call", (to or "").lower()))
        walk(t.get("to"), t.get("dataDecoded"), t.get("data"))
        return outl

    for role, addr, t, done in rows:
        dec = t.get("dataDecoded") or {}
        target = (t.get("to") or "").lower()
        lg = legs(t)
        # the approve is plumbing; the transaction is named after what it actually does, and where
        acts = [(v, a) for v, a in lg if v != "approve"] or lg
        label = " + ".join(dict.fromkeys(v for v, _ in acts))
        venue = next((names.get(a) for _, a in acts if names.get(a)), None)
        inner = next((a for _, a in acts if a), None)
        row = dict(safe_tx_hash=t.get("safeTxHash"), nonce=t.get("nonce"), safe=addr, safe_role=role,
                   confirmations=len(t.get("confirmations") or []), required=t.get("confirmationsRequired"),
                   submitted=t.get("submissionDate"), proposer=t.get("proposer"),
                   method=label or dec.get("method"), to=t.get("to"),
                   target_name=venue or names.get(inner or target) or names.get(target),
                   legs=[dict(verb=v, to=a, name=names.get(a)) for v, a in lg],
                   value=t.get("value"), executed=done, executed_at=t.get("executionDate"),
                   tx_hash=t.get("transactionHash"), successful=t.get("isSuccessful"))
        out.append(row)
    out.sort(key=lambda r: (r.get("nonce") or 0))
    return dict(client=slug, safes=nonces, queued=out)


def underlying(symbol: str | None, asset_group: str) -> str:
    """Token the bot command needs, from a Strategy API asset label."""
    s = (symbol or "").upper()
    for t in ("USDC", "USDT", "USDS", "GHO", "DAI", "EURC", "PYUSD", "RLUSD"):
        if t in s:
            return t
    if "WSTETH" in s:
        return "WSTETH"
    if asset_group == "ETH" or "ETH" in s:
        return "ETH"
    return s


def fmt_amount(usd: float, token: str, eth_price: float) -> str:
    import math
    if token in STABLES:
        return f"{math.floor(usd * 100) / 100:.2f}"        # never round up past the balance
    if not eth_price:
        raise ValueError("no ETH price in snapshot; re-run publish")
    return f"{math.floor(usd / eth_price * 1e6) / 1e6:.6f}"


def commands_for(move: dict, snap: dict) -> tuple[list[str], list[str]]:
    """Translate a simulator move into bot commands (same grammar as Telegram). Returns (commands, notes)."""
    notes, cmds = [], []
    grp = move["asset_group"]
    eth_price = fnum(snap.get("eth_price_usd"))
    frm, to = move["from"], move["to"]
    to_proto = PROTO.get(to["protocol"])
    if not to_proto:
        raise ValueError(f"no bot mapping for destination protocol {to['protocol']}")
    to_token = underlying(to.get("asset"), grp)
    amount = fmt_amount(fnum(move["amount_usd"]), to_token if to_token in STABLES else "ETH", eth_price)
    # deploying (almost) the whole idle balance or the whole position: use the bot's `all`, which
    # resolves the exact on-chain balance at build time instead of a snapshot figure
    if fnum(move["amount_usd"]) >= 0.99 * fnum(frm.get("usd")) and frm["kind"] in ("idle", "position"):
        amount = "all"
        notes.append("amount set to `all`: the bot resolves the exact balance on-chain when building")

    swap_leg = False
    swap_note = ("swapped on Uniswap v3 inside the same MultiSend (bot quote, 50 bps max slippage); "
                 "CoW cannot be bundled because it fills asynchronously")
    # 1. source leg
    if frm["kind"] == "idle":
        src_token = underlying(frm.get("symbol"), grp)
        if src_token in STABLES and to_token in STABLES and src_token != to_token:
            cmds.append(f"uniswap swap {amount} {src_token} {to_token}")   # permission engine rejects it where not allowed
            notes.append(f"{src_token} -> {to_token} {swap_note}")
            swap_leg = True
        if src_token == "WETH" and to_token == "ETH":
            cmds.append(f"cowswap unwrap {amount} WETH")
            notes.append("idle WETH unwrapped to ETH before staking")
    else:
        fp = PROTO.get(frm["protocol"])
        if not fp:
            raise ValueError(f"no bot mapping for source protocol {frm['protocol']}")
        src_token = underlying(frm.get("symbol"), grp)
        if grp == "ETH" and fp in ("stakewise", "etherfi", "stader", "lido", "rocketpool"):
            raise ValueError(f"exiting {fp} is an LST swap or an exit queue, not a withdraw; use the proposer bot (cowswap / unstake)")
        vault = f" {frm['vault']}" if frm.get("vault") and fp in ("morpho", "compound", "fluid", "gearbox") else ""
        if fp == "sky":
            cmds.append(f"sky withdraw {amount} USDS")
        else:
            cmds.append(f"{fp} withdraw {amount} {src_token}{vault}")
        if src_token != to_token:
            if src_token in STABLES and to_token in STABLES:
                cmds.append(f"uniswap swap {amount} {src_token} {to_token}")
                notes.append(f"{src_token} -> {to_token} {swap_note}")
                swap_leg = True
            else:
                raise ValueError(f"{src_token} -> {to_token} needs a swap between the legs; use the proposer bot (cowswap)")

    # 2. destination leg: after a swap, the worker sizes the deposit from the swap's minimum output
    if swap_leg:
        amount = "__SWAP_OUT__"
    vault = to.get("vault") or ""
    if to_proto == "morpho":
        if not vault:
            raise ValueError("Morpho deposit needs the vault address")
        cmds.append(f"morpho deposit {amount} {'WETH' if to_token == 'ETH' else to_token} {vault}")
        if to_token == "ETH":
            notes.append("Morpho ETH vaults take WETH; the bot wraps ETH when the WETH balance is short")
    elif to_proto in ("compound", "fluid", "gearbox"):
        cmds.append(f"{to_proto} deposit {amount} {'WETH' if to_token == 'ETH' else to_token}{(' ' + vault) if vault else ''}")
    elif to_proto == "sky":
        cmds.append(f"sky deposit {amount} USDS")
    elif to_proto in ("aave", "spark"):
        cmds.append(f"{to_proto} deposit {amount} {'WETH' if to_token == 'ETH' else to_token}")
    elif to_proto == "stakewise":
        cmds.append(f"stakewise stake {amount} ETH stakewise genesis")
    elif to_proto in ("etherfi", "stader", "rocketpool", "lido"):
        cmds.append(f"{to_proto} deposit {amount} ETH")
    else:
        raise ValueError(f"unsupported destination {to_proto}")
    return cmds, notes


_LAST_PULL = {"t": 0.0, "head": None}


def ensure_latest_proposer(max_age: int = 120) -> dict:
    """Fast-forward the kpk-labs/kpk-proposer checkout before use, so the executor always runs the
    code currently on kpk-labs main. Rate-limited to one pull per `max_age` seconds. Never resets or
    rewrites local runtime files (.env, Data/, .runtime are untracked in that repo)."""
    if not (SAFEAGENT / ".git").exists():
        return dict(tracked=False, note="SafeAgent folder is not a git checkout; code is whatever was deployed there")
    now = time.time()
    if now - _LAST_PULL["t"] < max_age and _LAST_PULL["head"]:
        return dict(tracked=True, head=_LAST_PULL["head"], pulled=False)
    g = lambda *a: subprocess.run(["git", "-C", str(SAFEAGENT), *a], capture_output=True, text=True, timeout=120)
    before = (g("rev-parse", "--short", "HEAD").stdout or "").strip()
    p = g("pull", "--ff-only", "-q", "origin", "main")
    after = (g("rev-parse", "--short", "HEAD").stdout or "").strip()
    _LAST_PULL.update(t=now, head=after)
    return dict(tracked=True, head=after, pulled=(before != after), error=(p.stderr or "").strip()[-200:] or None,
                remote=(g("remote", "get-url", "origin").stdout or "").strip())


def two_stage_commands(move: dict, snap: dict) -> tuple[list[str], dict, list[str]]:
    """Stable-to-stable rotation too big for an on-chain pool: stage 1 = withdraw + pre-signed CoW order,
    stage 2 = deposit the bought token once the order has filled. Returns (stage1_cmds, stage2, notes)."""
    grp = move["asset_group"]
    frm, to = move["from"], move["to"]
    eth_price = fnum(snap.get("eth_price_usd"))
    to_token = underlying(to.get("asset"), grp)
    src_token = underlying(frm.get("symbol"), grp)
    amount = fmt_amount(fnum(move["amount_usd"]), src_token if src_token in STABLES else "ETH", eth_price)
    if fnum(move["amount_usd"]) >= 0.99 * fnum(frm.get("usd")):
        amount = "all"
    cmds = []
    if frm["kind"] != "idle":
        fp = PROTO.get(frm["protocol"])
        vault = f" {frm['vault']}" if frm.get("vault") and fp in ("morpho", "compound", "fluid", "gearbox") else ""
        cmds.append(f"sky withdraw {amount} USDS" if fp == "sky" else f"{fp} withdraw {amount} {src_token}{vault}")
    cmds.append(f"cowswap swap {amount} {src_token} {to_token}")
    to_proto = PROTO.get(to["protocol"])
    vault = to.get("vault") or ""
    dep = {"morpho": f"morpho deposit all {to_token} {vault}", "compound": f"compound deposit all {to_token} {vault}".strip(),
           "fluid": f"fluid deposit all {to_token} {vault}".strip(), "gearbox": f"gearbox deposit all {to_token} {vault}".strip(),
           "sky": "sky deposit all USDS", "aave": f"aave deposit all {to_token}", "spark": f"spark deposit all {to_token}"}.get(to_proto)
    if not dep:
        raise ValueError(f"no stage-2 deposit command for {to_proto}")
    stage2 = dict(commands=[dep], label=f"deposit all {to_token} into {to['protocol']} {to.get('asset')}", wait_for=to_token,
                  client=move["client"], to=to)
    notes = [f"two-stage: stage 1 withdraws and places a pre-signed CoW order {src_token} -> {to_token} (fills after the Safe "
             f"executes); stage 2 deposits the {to_token} once it has arrived. The deposit cannot be bundled because CoW fills asynchronously."]
    return cmds, stage2, notes


def cow_groups(slug: str) -> list[dict]:
    """CoW swap groups (sell set, buy set) from the client's cached Roles permissions; TWAP groups excluded."""
    f = ROOT / "data" / f"{slug}.strategy.json"
    if not f.exists():
        return []
    raw = json.loads(f.read_text(encoding="utf-8")).get("raw_permissions") or {}
    out = []
    for e in (raw.get("allPermissions") or {}).get("cowswap") or (raw.get("permissions") or {}).get("cowswap") or []:
        if e.get("action") == "swap" and not e.get("isTWAP"):
            out.append(dict(sell=sorted(set(e.get("sellAssets") or [])), buy=sorted(set(e.get("buyAssets") or []))))
    return out


def cow_pair_ok(groups: list[dict], sell: str, buy: str) -> bool:
    s, b = sell.upper(), buy.upper()
    return any(s in {x.upper() for x in g["sell"]} and b in {x.upper() for x in g["buy"]} for g in groups)


def sweep_commands(body: dict, snap: dict) -> tuple[list[str], dict, list[str]]:
    """Rewards sweep: claim everything claimable, place one CoW order per reward token into USDC (stage 1),
    then deposit all USDC into the chosen venue (stage 2). Tokens below min_sweep_usd are skipped."""
    sw = snap.get("rewards_sweep") or {}
    min_usd = fnum(sw.get("min_usd") or 100)
    if body.get("items"):   # the page sends an explicit selection: honour it as is
        items = list(body["items"])
    else:
        items = [i for i in (sw.get("items") or []) if fnum(i.get("usd")) >= min_usd]
    if not items:
        raise ValueError(f"no reward worth sweeping (all below ${min_usd:,.0f})")
    notes = []
    claims = sorted({i["claim_cmd"] for i in items if i.get("claimable") and i.get("claim_cmd")} | set(body.get("extra_commands") or []))
    cmds = list(claims)
    by_tok: dict[str, float] = {}
    for i in items:
        by_tok[i["symbol"].upper()] = by_tok.get(i["symbol"].upper(), 0.0) + fnum(i.get("amount"))
    groups = cow_groups(body["client"])
    held_back, orders = [], []
    for sym, amt in by_tok.items():
        if sym in ("USDC",):
            continue
        if groups and not cow_pair_ok(groups, sym, "USDC"):
            allowed = sorted({b for g in groups if sym in {x.upper() for x in g["sell"]} for b in g["buy"]})
            held_back.append(f"{sym} (permitted buys: {', '.join(allowed) or 'none'})")
            continue
        cmds.append(f"cowswap swap {amt:.6f} {sym} USDC"); orders.append(sym)
    if held_back:
        notes.append("held in the Safe after claiming (no CoW route to USDC in this client's Roles permissions): " + "; ".join(held_back))
    to = body.get("to") or sw.get("best_usd_venue")
    if not to:
        raise ValueError("no permitted USDC venue to deposit into")
    to_proto = PROTO.get(to["protocol"])
    vault = to.get("vault") or ""
    dep = {"morpho": f"morpho deposit all USDC {vault}", "compound": f"compound deposit all USDC {vault}".strip(), "fluid": f"fluid deposit all USDC {vault}".strip(),
           "aave": "aave deposit all USDC", "spark": "spark deposit all USDC", "gearbox": f"gearbox deposit all USDC {vault}".strip()}.get(to_proto)
    if not dep:
        raise ValueError(f"no deposit command for {to_proto}")
    stage2 = dict(commands=[dep], label=f"deposit all USDC into {to['protocol']} {to.get('asset')}", wait_for="USDC", client=body["client"], to=to)
    notes.insert(0, "rewards sweep: stage 1 claims (" + (", ".join(claims) or "none") + f") and places {len(orders)} pre-signed CoW order(s) into USDC ({', '.join(orders) or 'none'}); "
                 "stage 2 deposits the USDC once the orders fill. `merkl claim` collects every pending Merkl token in one call; the CoW orders sell exactly the claimed amounts.")
    return cmds, stage2, notes


def run_worker(slug: str, op: str, payload: dict) -> dict:
    sync = ensure_latest_proposer()
    cdir = SAFEAGENT / CLIENT_DIRS[slug]
    if not (cdir / ".env").exists() or not (cdir / "proposal_planner.py").exists():
        return dict(error=f"SafeAgent client folder for {slug} not found or not configured: {cdir}")
    payload = dict(payload, plans_dir=str(PLANS))
    p = subprocess.run([sys.executable, str(HERE / "executor_worker.py"), "--client-dir", str(cdir), op],
                       input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8", timeout=420)
    txt = (p.stdout or "").strip().splitlines()
    for line in reversed(txt):
        if line.startswith("{"):
            try:
                res = json.loads(line)
                res["proposer_code"] = sync
                return res
            except json.JSONDecodeError:
                pass
    return dict(error="worker produced no JSON", stderr=(p.stderr or "")[-1500:], stdout=(p.stdout or "")[-500:])


RUNNING: set = set()
LOCK = threading.Lock()
GIT_LOCK = threading.Lock()


def git_publish(paths: list[str], message: str):
    """Commit the given data files and push. The box is the only writer of these files, so on a
    diverged remote we rebase and keep the box's versions. Returns True, or the error text."""
    def g(*a):
        return subprocess.run(["git", "-C", str(ROOT), *a], capture_output=True, text=True, timeout=180)
    with GIT_LOCK:
        g("config", "user.name", "kpk-rebalancer"); g("config", "user.email", "noreply@kpk.io")
        if (ROOT / ".git" / "rebase-merge").exists() or (ROOT / ".git" / "rebase-apply").exists():
            g("rebase", "--abort")
        g("add", *paths)
        if g("diff", "--staged", "--quiet").returncode == 0 and not g("rev-list", "--count", "@{u}..HEAD").stdout.strip().strip("0"):
            return True  # nothing new
        if g("diff", "--staged", "--quiet").returncode != 0:
            c = g("commit", "-q", "-m", message)
            if c.returncode != 0:
                return f"commit failed: {(c.stderr or c.stdout)[-300:]}"
        f = g("fetch", "-q", "origin")
        if f.returncode != 0:
            return f"fetch failed: {f.stderr[-300:]}"
        r = g("rebase", "-X", "theirs", "origin/main")
        if r.returncode != 0:
            g("rebase", "--abort")
            return f"rebase failed: {(r.stderr or r.stdout)[-300:]}"
        p = g("push", "-q", "origin", "main")
        return True if p.returncode == 0 else f"push failed: {p.stderr[-300:]}"


def refresh_client(slug: str) -> dict:
    """Full pipeline for one client on this host, then publish and push data/ to the repo so the
    Pages site updates. Strategy API pieces fall back to the last snapshot when unreachable."""
    with LOCK:
        if slug in RUNNING:
            return dict(error=f"refresh already running for {slug}")
        RUNNING.add(slug)
    log, t0 = [], time.time()
    try:
        def step(args, timeout=900):
            p = subprocess.run([sys.executable, *args], cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=timeout)
            tail = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()[-25:]
            log.append(dict(cmd=" ".join(Path(a).name if str(a).endswith(".py") else str(a) for a in args), rc=p.returncode, tail=tail))
            return p.returncode
        rc = step([str(HERE / "run.py"), "--client", slug])
        if rc not in (0, 2):
            return dict(ok=False, client=slug, error="run.py failed", log=log)
        step([str(HERE / "refresh_holdings.py"), "--clients", slug])
        step([str(HERE / "publish.py"), "--clients", slug])
        pushed = None
        if (ROOT / ".git").exists():
            pushed = git_publish([f"data/index.json", f"data/{slug}.json", f"data/{slug}.live.json"], f"data: {slug} refresh from {socket.gethostname()}")
        snap = json.loads((ROOT / "data" / f"{slug}.json").read_text(encoding="utf-8"))
        return dict(ok=True, client=slug, nav_tied=(rc == 0), seconds=round(time.time() - t0), pushed=pushed,
                    as_of=snap.get("as_of"), nav_usd=snap.get("nav_usd"), stale_note=snap.get("stale_note"), flags=snap.get("flags"), log=log)
    except Exception as e:
        return dict(ok=False, client=slug, error=f"{type(e).__name__}: {e}", log=log)
    finally:
        with LOCK:
            RUNNING.discard(slug)


def load_snapshot(slug: str) -> dict:
    snap = json.loads((ROOT / "data" / f"{slug}.json").read_text(encoding="utf-8"))
    live = ROOT / "data" / f"{slug}.live.json"
    if not snap.get("eth_price_usd") and live.exists():
        snap["eth_price_usd"] = json.loads(live.read_text(encoding="utf-8")).get("eth_price_usd")
    return snap


class H(BaseHTTPRequestHandler):
    def _cors(self):
        origin = self.headers.get("Origin", "")
        if any(origin.startswith(o) for o in ALLOWED_ORIGINS):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        # Chrome's Private Network Access: a public https page reaching 127.0.0.1 needs this on the preflight
        self.send_header("Access-Control-Allow-Private-Network", "true")

    STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
                    ".json": "application/json", ".png": "image/png", ".svg": "image/svg+xml", ".woff2": "font/woff2", ".ico": "image/x-icon"}

    def _is_local(self) -> bool:
        """A genuinely local caller: loopback socket AND not relayed by the reverse proxy. Caddy connects from
        127.0.0.1 on behalf of the public internet but always stamps X-Forwarded-For, so proxied requests are
        never 'local' whatever their socket address says. Browsers add Origin on cross-site fetches; scripts do not,
        which is why the old 'loopback without Origin' rule let any curl through the public URL."""
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            return False
        for h in ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "X-Real-IP", "Forwarded"):
            if self.headers.get(h):
                return False
        return not self.headers.get("Origin")

    def _authorized(self) -> bool:
        tok = os.environ.get("EXECUTOR_TOKEN", "")
        if not tok:
            return self._is_local()                                   # no token configured: local callers only
        return self.headers.get("Authorization", "") == f"Bearer {tok}" or self._is_local()

    def _static(self, rel: str) -> bool:
        """Serve the page itself from the repo root so a tunnelled http://127.0.0.1:8743/ is same-origin."""
        rel = rel.split("?")[0].lstrip("/") or "index.html"
        f = (ROOT / rel).resolve()
        if ".." in rel or not str(f).startswith(str(ROOT.resolve())) or f.suffix not in self.STATIC_TYPES or not f.is_file():
            return False
        body = f.read_bytes()
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", self.STATIC_TYPES[f.suffix])
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return True

    def _json(self, code: int, obj):
        body = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def do_GET(self):
        if self.path.startswith("/health"):
            reg = registry()
            clients, propose = [], {}
            for slug in reg["clients"]:
                cdir = SAFEAGENT / CLIENT_DIRS.get(slug, "")
                envf = cdir / ".env"
                if envf.exists() and (cdir / "proposal_planner.py").exists():
                    clients.append(slug)
                    keys = {l.split("=", 1)[0].strip() for l in envf.read_text(encoding="utf-8", errors="replace").splitlines() if "=" in l and not l.strip().startswith("#")}
                    propose[slug] = "AGENT_PRIVATE_KEY" in keys
            return self._json(200, dict(ok=True, host=socket.gethostname(), clients=clients, propose=propose, safeagent=str(SAFEAGENT), auth=bool(os.environ.get("EXECUTOR_TOKEN")),
                                        proposer_code=ensure_latest_proposer(),
                                        refresh_running=sorted(RUNNING), can_push=(ROOT / ".git").exists()))
        if self.path.startswith("/data/"):
            name = self.path.split("/data/", 1)[1].split("?")[0]
            f = ROOT / "data" / name
            if not name or "/" in name or ".." in name or not f.exists():
                return self._json(404, dict(error="not found"))
            return self._json(200, json.loads(f.read_text(encoding="utf-8")))
        if self.path.startswith("/live/"):
            if not self._authorized():      # spends DeBank + vaults.fyi quota: bearer token required, like /refresh
                return self._json(401, dict(error="unauthorized: set the executor token on the page"))
            slug = self.path.split("/live/", 1)[1].split("?")[0]
            if slug not in CLIENT_DIRS:
                return self._json(404, dict(error="unknown client"))
            try:
                return self._json(200, live_view(slug, force="force" in self.path))
            except Exception as e:
                return self._json(502, dict(error=f"live view failed: {str(e)[:200]}"))
        if self.path.startswith("/queue/"):
            slug = self.path.split("/queue/", 1)[1].split("?")[0]
            if slug not in CLIENT_DIRS:
                return self._json(404, dict(error="unknown client"))
            try:
                snap = load_snapshot(slug)
                safe = (snap.get("safes") or {}).get("avatar") or snap.get("avatar_safe")
                return self._json(200, safe_queue(int(snap.get("chain_id") or 1), safe, slug))
            except Exception as e:
                return self._json(502, dict(error=f"safe queue lookup failed: {str(e)[:160]}"))
        if self.path.startswith("/safe-tx/"):
            parts = self.path.split("/safe-tx/", 1)[1].split("?")[0].split("/")
            if len(parts) != 2 or parts[0] not in CLIENT_DIRS:
                return self._json(400, dict(error="use /safe-tx/<client>/<safeTxHash>"))
            try:
                snap = load_snapshot(parts[0])
                st = safe_tx_status(int(snap.get("chain_id") or 1), parts[1])
                if st["is_executed"]:
                    with LIVE_LOCK:
                        LIVE_CACHE.pop(parts[0], None)     # balances moved: the next live view must rebuild
                return self._json(200, st)
            except Exception as e:
                return self._json(502, dict(error=f"safe tx lookup failed: {str(e)[:160]}"))
        if self.path.startswith("/balances/"):
            # live Safe balances for the swap panel (Safe Transaction Service, key held on the box)
            slug = self.path.split("/balances/", 1)[1].split("?")[0]
            if slug not in CLIENT_DIRS:
                return self._json(404, dict(error="unknown client"))
            try:
                from fetch_holdings import fetch_safe_balances
                snap = load_snapshot(slug)
                chain_id = int(snap.get("chain_id") or 1)
                avatar = (snap.get("safes") or {}).get("avatar") or snap.get("avatar_safe")
                rows = fetch_safe_balances(registry(), chain_id, avatar)
                # the tx-service list carries spam tokens (fake "ETH"/"ERC20" with absurd balances): keep only tokens
                # the bot's registry knows, matched by ADDRESS, and report them under the registry symbol
                _, addr = known_tokens(slug)
                by_addr = {a.lower(): sym for sym, a in addr.items()}
                native_sym = next((s for s, a in addr.items() if a.lower() in (NATIVE, "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee")), "ETH")
                out = []
                for r in rows:
                    if r["balance"] <= 0:
                        continue
                    if r["token"] == NATIVE:
                        out.append(dict(symbol=native_sym, token=NATIVE, balance=r["balance"], decimals=18, safe_symbol=r["symbol"]))
                    elif r["token"] in by_addr:
                        out.append(dict(symbol=by_addr[r["token"]], token=r["token"], balance=r["balance"], decimals=r["decimals"], safe_symbol=r["symbol"]))
                return self._json(200, dict(client=slug, chain_id=chain_id, safe=avatar, fetched_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                            balances=out, filtered_out=len([r for r in rows if r["balance"] > 0]) - len(out)))
            except Exception as e:
                return self._json(502, dict(error=f"safe balances: {str(e)[:160]}"))
        if self.path.startswith("/swap-pairs/"):
            slug = self.path.split("/swap-pairs/", 1)[1].split("?")[0]
            f = ROOT / "data" / f"{slug}.strategy.json"
            if slug not in CLIENT_DIRS or not f.exists():
                return self._json(404, dict(error="no strategy cache for this client"))
            raw = json.loads(f.read_text(encoding="utf-8")).get("raw_permissions") or {}
            groups = cow_groups(slug)
            known, _ = known_tokens(slug)
            return self._json(200, dict(client=slug, groups=groups, known_tokens=known,
                                        cached_at=json.loads(f.read_text(encoding="utf-8")).get("vault_data_fetched_at")))
        if self.path.startswith("/cow/"):
            uid = self.path.split("/cow/", 1)[1].split("?")[0]
            if not uid.startswith("0x") or len(uid) < 20:
                return self._json(400, dict(error="bad order uid"))
            try:
                import urllib.request
                with urllib.request.urlopen(f"https://api.cow.fi/mainnet/api/v1/orders/{uid}", timeout=20) as r:
                    o = json.loads(r.read())
                return self._json(200, dict(uid=uid, status=o.get("status"), executed_sell=o.get("executedSellAmount"),
                                            executed_buy=o.get("executedBuyAmount"), valid_to=o.get("validTo"),
                                            invalidated=o.get("invalidated"), url=f"https://explorer.cow.fi/orders/{uid}"))
            except Exception as e:
                return self._json(502, dict(error=str(e)[:200]))
        if self.path.startswith("/refresh/"):
            if not self._authorized():
                return self._json(401, dict(error="unauthorized: set the executor token on the page"))
            slug = self.path.split("/refresh/", 1)[1].split("?")[0]
            if slug not in CLIENT_DIRS:
                return self._json(400, dict(error=f"unknown client {slug}"))
            return self._json(200, refresh_client(slug))
        if self._static(self.path):
            return
        self._json(404, dict(error="not found"))

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, dict(error="bad json"))
        if self.path.startswith(("/plan", "/propose", "/quote", "/cow/resubmit")) and not self._authorized():
            return self._json(401, dict(error="unauthorized: set the executor token on the page"))
        if self.path.startswith("/cow/resubmit"):
            # recovery: re-place an order a Safe tx already pre-signed on-chain but that never reached the CoW API
            slug = body.get("client")
            if slug not in CLIENT_DIRS:
                return self._json(400, dict(error=f"unknown client {slug}"))
            res = run_worker(slug, "resubmit", dict(tx_hash=body.get("tx_hash"), dry_run=bool(body.get("dry_run"))))
            return self._json(200 if not res.get("error") else 422, res)
        if self.path.startswith("/quote"):
            slug = body.get("client")
            if slug not in CLIENT_DIRS:
                return self._json(400, dict(error=f"unknown client {slug}"))
            res = run_worker(slug, "quote", dict(sell=body.get("sell"), buy=body.get("buy"), amount=body.get("amount")))
            return self._json(200 if not res.get("error") else 422, res)
        if self.path.startswith("/plan"):
            slug = body.get("client")
            if slug not in CLIENT_DIRS:
                return self._json(400, dict(error=f"unknown client {slug}"))
            if body.get("rewards_sweep"):
                try:
                    snap = load_snapshot(slug)
                    cmds, stage2, notes = sweep_commands(body, snap)
                except Exception as e:
                    return self._json(400, dict(error=str(e)))
                res = run_worker(slug, "plan", dict(commands=cmds, move=body))
                res.setdefault("commands", cmds); res["notes"] = notes; res["stage"] = "1 of 2"; res["next_stage"] = stage2
                return self._json(200 if not res.get("error") else 422, res)
            if body.get("commands"):   # stage 2 (or any explicit command list) from the page
                res = run_worker(slug, "plan", dict(commands=list(body["commands"]), move=body.get("move") or {}))
                res.setdefault("commands", body["commands"]); res["notes"] = body.get("notes") or []; res["stage"] = body.get("stage")
                return self._json(200 if not res.get("error") else 422, res)
            try:
                snap = load_snapshot(slug)
                cmds, notes = commands_for(body, snap)
            except Exception as e:
                return self._json(400, dict(error=str(e)))
            res = run_worker(slug, "plan", dict(commands=cmds, move=body))
            if res.get("error") and "on-chain swap not viable" in res["error"]:
                rejection = res["error"]
                try:
                    cmds, stage2, notes2 = two_stage_commands(body, snap)
                except Exception as e:
                    return self._json(422, dict(error=str(e)))
                res = run_worker(slug, "plan", dict(commands=cmds, move=body))
                res["stage"] = "1 of 2"; res["next_stage"] = stage2
                notes = notes2 + ["Uniswap route rejected: " + rejection.split(". Use a CoW")[0]]
            res.setdefault("commands", cmds)
            res["notes"] = notes
            return self._json(200 if not res.get("error") else 422, res)
        if self.path.startswith("/propose"):
            pid = str(body.get("plan_id") or "")
            pf = PLANS / f"{pid}.json"
            if not pid or not pf.exists():
                return self._json(404, dict(error="plan not found"))
            rec = json.loads(pf.read_text(encoding="utf-8"))
            slug = next((s for s, d in CLIENT_DIRS.items() if Path(rec["client_dir"]).name == d), None)
            if not slug:
                return self._json(400, dict(error="plan has no known client"))
            res = run_worker(slug, "propose", dict(plan_file=str(pf)))
            return self._json(200 if not res.get("error") else 422, res)
        self._json(404, dict(error="not found"))

    def log_message(self, fmt, *args):
        sys.stderr.write("executor: " + (fmt % args) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8743)
    ap.add_argument("--safeagent", default=None)
    ap.add_argument("--hourly-live", action="store_true", help="refresh live holdings for all clients every hour and push")
    a = ap.parse_args()
    global SAFEAGENT
    if a.safeagent:
        SAFEAGENT = Path(a.safeagent)
    PLANS.mkdir(parents=True, exist_ok=True)
    load_env()
    print("auth:", "token required for plan/propose/refresh" if os.environ.get("EXECUTOR_TOKEN") else "NO EXECUTOR_TOKEN set: loopback callers only")
    if a.hourly_live:
        def loop():
            while True:
                time.sleep(3600)
                try:
                    subprocess.run([sys.executable, str(HERE / "refresh_holdings.py")], cwd=str(ROOT), timeout=900)
                    if (ROOT / ".git").exists():
                        r = git_publish([str(p.relative_to(ROOT)) for p in (ROOT / "data").glob("*.live.json")], "data: hourly live holdings")
                        if r is not True:
                            sys.stderr.write(f"hourly push: {r}\n")
                except Exception as e:
                    sys.stderr.write(f"hourly live refresh failed: {e}\n")
        threading.Thread(target=loop, daemon=True).start()
    print(f"executor on http://127.0.0.1:{a.port}  (SafeAgent: {SAFEAGENT})")
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()


if __name__ == "__main__":
    main()
