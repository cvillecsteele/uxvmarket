"""Category code + serial → permanent vendor designation.

Designations are persisted across runs in `state/designations.json` so that
once `Vertiq` becomes `A.0042`, it stays `A.0042` forever. The generator is
idempotent: re-running against an unchanged DB produces the same output.

Existing designations are stable and are not re-keyed when categories move.
Only newly assigned vendors use the expanded category letter mapping.
"""

from __future__ import annotations

import json
from pathlib import Path

# fe/generator/generator/designations.py → generator/ → fe/generator
GENERATOR_ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = GENERATOR_ROOT / "state"
DESIG_PATH = STATE_DIR / "designations.json"

CATEGORY_LETTERS: dict[str, str] = {
    "propulsion-electronics": "A",
    "propulsion-mechanical":  "B",
    "power-systems":          "C",
    "flight-vehicle-control": "D",
    "sensors-navigation":     "E",
    "isr-payloads":           "F",
    "electronic-warfare":     "G",
    "munitions":              "H",
    "communications":         "I",
    "mechanical-subsystems":  "J",
    "structures-materials":   "K",
    "airframes":              "L",
    "recovery-systems":       "M",
    "flight-termination":     "N",
    "ground-segment":         "O",
    "test-measurement":       "P",
}


def load() -> dict[str, str]:
    if DESIG_PATH.exists():
        return json.loads(DESIG_PATH.read_text())
    return {}


def save(designations: dict[str, str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    DESIG_PATH.write_text(json.dumps(designations, indent=2, sort_keys=True) + "\n")


def assign(player_id: int | str, sector_slug: str, state: dict[str, str]) -> str:
    """Return the existing designation for `player_id`, or assign a new one.

    Mutates `state` in place when a new designation is created. Caller is
    responsible for persisting via `save()` after the run.
    """
    key = str(player_id)
    if key in state:
        return state[key]

    letter = CATEGORY_LETTERS.get(sector_slug, "X")
    used_serials = {
        int(d.split(".")[1])
        for d in state.values()
        if d.startswith(letter + ".") and "." in d
    }
    serial = 1
    while serial in used_serials:
        serial += 1
    designation = f"{letter}.{serial:04d}"
    state[key] = designation
    return designation
