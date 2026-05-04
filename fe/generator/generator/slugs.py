"""Canonical-name → URL slug derivation.

Rules (per memory: project_fe_ia_locked.md):
- Strip parenthetical suffixes (e.g. "Plasma Ruggedized Solutions (PRS)" → "Plasma Ruggedized Solutions").
- NFKD-fold then ASCII (handles diacritics like "Zlín" → "Zlin").
- Lowercase.
- Strip corporate suffixes (Inc., LLC, Ltd, Corp, Corporation, GmbH, S.A., Co.).
- Replace any non-alphanumeric run with a single hyphen.
- Trim leading/trailing hyphens.
- Truncate to 60 chars.
"""

from __future__ import annotations

import re
import unicodedata

CORPORATE_SUFFIX_RE = re.compile(
    r"\b("
    r"inc|incorporated|llc|l\.l\.c\.|"
    r"ltd|limited|"
    r"corp|corporation|"
    r"co|company|"
    r"gmbh|"
    r"sa|s\.a\.|"
    r"plc|"
    r"ag|"
    r"bv|b\.v\."
    r")\.?$",
    re.IGNORECASE,
)


def slugify(name: str, max_len: int = 60) -> str:
    if not name or not name.strip():
        return "unnamed"
    s = name.strip()
    # Strip trailing parentheticals (e.g. "Foo (bar)").
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s).strip()
    # NFKD then ASCII (drop diacritics, fold compat chars like ™).
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    # Strip corporate suffixes after ASCII fold.
    s = CORPORATE_SUFFIX_RE.sub("", s).strip()
    # Lowercase.
    s = s.lower()
    # Replace non-alphanumeric runs with hyphens.
    s = re.sub(r"[^a-z0-9]+", "-", s)
    # Trim hyphens.
    s = s.strip("-")
    # Truncate.
    if len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s or "unnamed"
