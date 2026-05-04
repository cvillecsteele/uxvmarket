"""Render one category as Markdown with YAML frontmatter for fe/site."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from generator.designations import CATEGORY_LETTERS
from generator.sectors import db_slug_to_fe_slug


def render(sector_row: dict[str, Any] | Any, out_dir: Path) -> Path:
    """Write `<slug>.md` to `out_dir`. Idempotent."""
    db_slug = sector_row["slug"]
    fe_slug = db_slug_to_fe_slug(db_slug)
    out_path = out_dir / f"{fe_slug}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    letter = CATEGORY_LETTERS.get(fe_slug)

    frontmatter: dict[str, Any] = {
        "letter": letter,
        "display_name": sector_row["display_name"],
        "is_meta": bool(sector_row["is_meta"]),
        "intro_paragraph": sector_row["description"] or "",
    }

    cleaned = {key: value for key, value in frontmatter.items() if value is not None}
    body = "---\n" + yaml.safe_dump(cleaned, sort_keys=False, allow_unicode=True, width=120) + "---\n"
    out_path.write_text(body, encoding="utf-8")
    return out_path
