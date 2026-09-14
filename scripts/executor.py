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
ALLOWED_ORIGINS = ("http://localhost:", "http://127.0.0.1:", "https://jktranslator.github.io", "https://82-70-94-93.sslip.io")
PROTECTED = ("/plan", "/propose", "/refresh/")   # need Authorization: Bearer <EXECUTOR_TOKEN>


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

    def _authorized(self) -> bool:
        tok = os.environ.get("EXECUTOR_TOKEN", "")
        if not tok:
            return self.client_address[0] in ("127.0.0.1", "::1")   # no token configured: loopback only
        return self.headers.get("Authorization", "") == f"Bearer {tok}" or self.client_address[0] in ("127.0.0.1", "::1") and not self.headers.get("Origin")

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
        if self.path.startswith(("/plan", "/propose")) and not self._authorized():
            return self._json(401, dict(error="unauthorized: set the executor token on the page"))
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
