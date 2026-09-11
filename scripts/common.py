"""Shared helpers for the rebalancer scripts: env loading, HTTP, client registry.

Keys are read from the environment first, then from `.env.local` next to this skill, then from
the Hypernative workspace `.env.local` (the team's existing secrets file). Nothing is ever
written back or printed. Override the fallback file with KPK_ENV_FILE=<path>.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

for _s in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252; token symbols are not
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SKILL_DIR = Path(__file__).resolve().parent.parent   # .../Karpatkey/rebalancer
REGISTRY_PATH = SKILL_DIR / "clients.json"
WORKSPACE = SKILL_DIR.parent  # .../Karpatkey
FALLBACK_ENV_FILES = [
    SKILL_DIR / ".env.local",
    WORKSPACE / "Claude" / "Hypernative" / ".env.local",
]


def load_env() -> None:
    files = []
    if os.environ.get("KPK_ENV_FILE"):
        files.append(Path(os.environ["KPK_ENV_FILE"]))
    files += FALLBACK_ENV_FILES
    for path in files:
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def key(name: str, required: bool = False) -> str:
    v = os.environ.get(name, "").strip()
    if required and not v:
        sys.exit(f"ERROR: {name} is not set. Export it or add it to {FALLBACK_ENV_FILES[0]} "
                 f"(names only in .env.example; never commit .env.local).")
    return v


def registry() -> dict:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def client(slug: str) -> dict:
    reg = registry()
    if slug not in reg["clients"]:
        sys.exit(f"ERROR: unknown client {slug!r}. Known: {', '.join(reg['clients'])}")
    c = dict(reg["clients"][slug])
    c["slug"] = slug
    return c


def http_json(url: str, *, headers: dict | None = None, data: dict | None = None,
              timeout: int = 120, retries: int = 3):
    body = json.dumps(data).encode() if data is not None else None
    hdrs = {"accept": "application/json", "User-Agent": "kpk-rebalancer/1.0 (+python-urllib)"}
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            txt = e.read()[:300].decode(errors="replace")
            if e.code in (400, 401, 402, 403, 404):
                raise RuntimeError(f"HTTP {e.code} {url}: {txt}") from None
            if e.code == 429:
                ra = (e.headers.get("Retry-After") or "").strip() if e.headers else ""
                time.sleep(min(float(ra), 60.0) if ra.isdigit() else 12.0 * (attempt + 1))
            else:
                time.sleep(1.5 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"failed after {retries} attempts: {url}: {last}")


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")


def fnum(x, default=0.0) -> float:
    try:
        return float(x) if x is not None else default
    except (TypeError, ValueError):
        return default


def asset_group_of(symbol: str, reg: dict) -> str:
    s = (symbol or "").strip()
    for grp, syms in reg["asset_groups"].items():
        if s in syms or s.upper() in [x.upper() for x in syms]:
            return grp
    su = s.upper()
    if "USD" in su or su in ("GHO", "DAI"):
        return "USD"
    if "ETH" in su:
        return "ETH"
    if "EUR" in su:
        return "EURO"
    return "OTHER"
