from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return slug[:80] or "item"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    return "\n".join(line for line in (s.strip() for s in soup.get_text("\n").splitlines()) if line)


def markdown_to_text(markdown: str) -> str:
    return "\n".join(line.rstrip() for line in markdown.splitlines() if line.strip())


def text_from_markdown_or_html(markdown: str | None, html: str | None) -> str:
    if markdown:
        return markdown_to_text(markdown)
    if html:
        return html_to_text(html)
    return ""


def write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def write_bytes(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def parse_pdf_text(path: Path) -> str | None:
    try:
        from pypdf import PdfReader
    except ModuleNotFoundError:
        return None
    try:
        reader = PdfReader(str(path))
        parts = [(page.extract_text() or "").strip() for page in reader.pages]
    except Exception:
        return None
    text = "\n\n".join(part for part in parts if part)
    return text or None


def json_safe_browserless_response(raw: dict[str, Any], *, local_metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "browserless": raw,
        "local_metadata": local_metadata,
    }

