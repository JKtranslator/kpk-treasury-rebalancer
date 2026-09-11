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
import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from common import client, fnum, registry

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
WORKSPACE = ROOT.parent
SAFEAGENT = WORKSPACE / "Codex" / "SafeAgentAll"
PLANS = ROOT / "runs" / "plans"

# rebalancer client slug -> SafeAgent client folder
CLIENT_DIRS = {"ens": "ens", "nexus": "nexus", "cow": "cow dao", "balancer": "balancer"}

# Strategy API protocol key -> bot protocol word
PROTO = {"morphoVaults": "morpho", "aave_v3": "aave", "compound_v3": "compound", "fluid": "fluid", "sky": "sky",
         "spark": "spark", "stakewise_v3": "stakewise", "lido": "lido", "ether_fi": "etherfi", "stader": "stader",
         "rocket_pool": "rocketpool", "gearbox": "gearbox"}
STABLES = {"USDC", "USDT", "USDS", "DAI", "GHO", "EURC", "PYUSD", "RLUSD"}
ALLOWED_ORIGINS = ("http://localhost:", "http://127.0.0.1:", "https://jktranslator.github.io")


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
    if token in STABLES:
        return f"{usd:.2f}"
    if not eth_price:
        raise ValueError("no ETH price in snapshot; re-run publish")
    return f"{usd / eth_price:.6f}"


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

    # 1. source leg
    if frm["kind"] == "idle":
        src_token = underlying(frm.get("symbol"), grp)
        if src_token in STABLES and to_token in STABLES and src_token != to_token:
            raise ValueError(f"idle {src_token} into a {to_token} venue needs a swap first; use the proposer bot (cowswap)")
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
            raise ValueError(f"{src_token} -> {to_token} needs a swap between the legs; use the proposer bot (cowswap)")

    # 2. destination leg
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


def run_worker(slug: str, op: str, payload: dict) -> dict:
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
                return json.loads(line)
            except json.JSONDecodeError:
                pass
    return dict(error="worker produced no JSON", stderr=(p.stderr or "")[-1500:], stdout=(p.stdout or "")[-500:])


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
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

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
            return self._json(200, dict(ok=True, clients=clients, propose=propose, safeagent=str(SAFEAGENT)))
        self._json(404, dict(error="not found"))

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, dict(error="bad json"))
        if self.path.startswith("/plan"):
            slug = body.get("client")
            if slug not in CLIENT_DIRS:
                return self._json(400, dict(error=f"unknown client {slug}"))
            try:
                snap = load_snapshot(slug)
                cmds, notes = commands_for(body, snap)
            except Exception as e:
                return self._json(400, dict(error=str(e)))
            res = run_worker(slug, "plan", dict(commands=cmds, move=body))
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
    a = ap.parse_args()
    global SAFEAGENT
    if a.safeagent:
        SAFEAGENT = Path(a.safeagent)
    PLANS.mkdir(parents=True, exist_ok=True)
    print(f"executor on http://127.0.0.1:{a.port}  (SafeAgent: {SAFEAGENT})")
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()


if __name__ == "__main__":
    main()
