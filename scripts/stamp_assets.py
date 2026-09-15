"""Stamp index.html asset links with a content hash (?v=<sha1[:10]>) so browsers and GitHub Pages
fetch the new app.js / css after every deploy. Run after editing anything under assets/:
    python scripts/stamp_assets.py
Deterministic: unchanged assets keep their stamp, so the box's working tree never drifts."""
import hashlib, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def main() -> int:
    h = hashlib.sha1()
    for name in ("app.js", "rebalancer.css", "styles.css"):
        h.update((ROOT / "assets" / name).read_bytes())
    v = h.hexdigest()[:10]
    p = ROOT / "index.html"
    t = p.read_text(encoding="utf-8")
    t2 = re.sub(r'assets/(app\.js|rebalancer\.css|styles\.css)(\?v=[0-9a-f]+)?', lambda m: f"assets/{m.group(1)}?v={v}", t)
    if t2 != t:
        p.write_text(t2, encoding="utf-8"); print(f"index.html stamped v={v}")
    else:
        print(f"already stamped v={v}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
