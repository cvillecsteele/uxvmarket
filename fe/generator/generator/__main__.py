"""Generator entry point.

Run with `uv run python -m generator` from `fe/generator/`,
or via `make site` from the project root.

Reads `vendors/<slug>/{profile,products}.json` and writes:
- `fe/site/src/content/vendor/<slug>.md` — one per categorized player
- `fe/site/src/content/sector/<slug>.md` — one per public category
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil

from generator import canonical, designations
from generator.render import sector as sector_render
from generator.render import vendor as vendor_render

# fe/generator/generator/__main__.py → generator/ → fe/generator → fe
FE_ROOT = Path(__file__).resolve().parents[2]
SITE_CONTENT = FE_ROOT / "site" / "src" / "content"


def main() -> int:
    vendors_root = Path(os.environ.get("UXV_VENDORS_ROOT", canonical.DEFAULT_VENDORS_ROOT)).expanduser()
    if not canonical.has_canonical_source(vendors_root):
        raise FileNotFoundError(f"canonical vendors source not found at {vendors_root}")
    return _main_canonical(vendors_root)


def _reset_content_dirs() -> None:
    for rel in ("vendor", "sector"):
        target = SITE_CONTENT / rel
        if target.exists():
            shutil.rmtree(target)


def _main_canonical(vendors_root: Path) -> int:
    print(f"[generator] reading canonical vendors from {vendors_root}", flush=True)
    desig_state = designations.load()
    _reset_content_dirs()

    category_count = 0
    for category in canonical.all_categories():
        sector_render.render(category, SITE_CONTENT / "sector")
        category_count += 1
    print(f"[generator] wrote {category_count} category files", flush=True)

    vendors = canonical.load_vendors(vendors_root, desig_state)
    for vendor_data in vendors:
        vendor_render.render(vendor_data, SITE_CONTENT / "vendor")
    print(f"[generator] wrote {len(vendors)} vendor files", flush=True)

    designations.save(desig_state)
    print("[generator] done.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
