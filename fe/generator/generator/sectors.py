"""Player → sector slug mapping.

The DB doesn't (yet) store a direct player→category relationship. This
mapping lives in `state/player_sectors.json` and is hand-curated. Any
player_id missing from the file is treated as uncategorized — the
generator emits a warning and skips that vendor (no content file).

Sector slug values match the canonical 12 sectors in the IA memory:
propulsion-electronics, propulsion-mechanical, power-systems,
flight-vehicle-control, sensors-navigation, payloads, communications,
mechanical-subsystems, structures-materials, recovery-systems,
ground-segment, test-measurement.
"""

from __future__ import annotations

import json
from pathlib import Path

GENERATOR_ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = GENERATOR_ROOT / "state"
PLAYER_SECTORS_PATH = STATE_DIR / "player_sectors.json"


def load() -> dict[str, str]:
    """Return mapping of player_id (str) → sector_slug. Empty if file missing."""
    if PLAYER_SECTORS_PATH.exists():
        return json.loads(PLAYER_SECTORS_PATH.read_text())
    return {}


def db_slug_to_fe_slug(db_slug: str) -> str:
    """The DB stores subsystem_categories.slug with underscores
    (`propulsion_electronics`); the fe/site/ schema and URL grammar use
    hyphens. Translate."""
    return db_slug.replace("_", "-")
