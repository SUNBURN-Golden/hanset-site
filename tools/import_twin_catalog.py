"""Copy the digital-twin public export into the repo, sanitized.

Usage: python3 tools/import_twin_catalog.py /workspace/hanset-data/twin/export/public_catalog.json
Keeps only venues that already have a public page, and only slot/value/status/source/check-time keys.
No contacts, addresses, quotes or internal ids are copied.
"""
import json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
KEEP = ("entity_type", "kind", "attribute", "qualifiers", "value", "unit", "status", "source_url", "confirmed_at")
src = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
pages = set(json.loads((ROOT / "site/content/venues_en.json").read_text(encoding="utf-8")))
out = {"schema_version": 1, "generated_at": src["generated_at"], "venues": {}}
for v in src["venues"]:
    slug = v.get("public_slug")
    if slug not in pages: continue
    rows = []
    for f in v["fields"]:
        r = {k: f.get(k) for k in KEEP}
        if r["status"] not in ("fact", "source_claim"): r["value"] = None  # never carry unverified/AI values
        rows.append(r)
    out["venues"][slug] = rows
p = ROOT / "site/content/twin_catalog.json"
p.write_text(json.dumps(out, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
print(f"wrote {p} ({len(out['venues'])} venues, {sum(map(len, out['venues'].values()))} fields)")
