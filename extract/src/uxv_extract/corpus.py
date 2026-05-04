from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Resource:
    resource_id: str
    url: str
    final_url: str | None
    page_class: str
    depth: int
    text_path: Path | None
    markdown_path: Path | None
    raw_html_path: Path | None
    json_path: Path | None

    @property
    def stem(self) -> str:
        for path in (self.text_path, self.markdown_path, self.raw_html_path, self.json_path):
            if path is not None:
                return path.stem
        return ""


@dataclass(frozen=True)
class SkippedEntry:
    url: str
    page_class: str
    status: str
    skip_reason: str | None
    depth: int
    discovered_from: tuple[str, ...]


class CorpusReader:
    """Read-only view over a single mirroring target directory.

    Layout (per `mirroring/` package):
        <corpus_root>/
            manifest.json
            crawl_index.json
            quality_report.json
            text/      NNNN-*.txt
            markdown/  NNNN-*.md
            raw/       NNNN-*.html
            json/      NNNN-*.json

    `resource-NNNN` in the crawl_index maps to files prefixed `NNNN-` in each
    of the four format directories. Some formats may be missing for a given
    resource (e.g. KML resources have json+text only).
    """

    def __init__(self, corpus_root: Path, manifest: dict[str, Any]) -> None:
        self.corpus_root = corpus_root
        self._manifest = manifest

    @classmethod
    def load(cls, corpus_root: Path | str) -> CorpusReader:
        corpus_root = Path(corpus_root)
        manifest_path = corpus_root / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"manifest.json not found at {manifest_path}")
        with manifest_path.open() as f:
            manifest = json.load(f)
        return cls(corpus_root=corpus_root, manifest=manifest)

    @classmethod
    def from_workspace(
        cls,
        *,
        workspace_root: Path | str,
        run_id: str,
        target_id: str,
    ) -> CorpusReader:
        corpus_root = (
            Path(workspace_root)
            / "output"
            / "runs"
            / run_id
            / "targets"
            / target_id
        )
        return cls.load(corpus_root)

    @classmethod
    def from_vendor_canonical(
        cls,
        *,
        vendors_root: Path | str,
        slug: str,
    ) -> CorpusReader:
        """Read the canonical website corpus produced by `uxv-mirror promote`.

        Layout: `<vendors_root>/<slug>/website/manifest.json`. Resource IDs
        here are stable across mirror rounds, so citations produced against
        canonical don't need migration when the next round adds new pages.
        """
        return cls.load(Path(vendors_root) / slug / "website")

    @property
    def target_id(self) -> str:
        return self._manifest["target"]["target_id"]

    @property
    def display_name(self) -> str:
        return self._manifest["target"]["display_name"]

    @property
    def homepage_url(self) -> str:
        return self._manifest["target"]["homepage_url"]

    @property
    def run_id(self) -> str:
        return self._manifest["run_id"]

    @property
    def quality_status(self) -> str:
        return self._manifest.get("quality_report", {}).get("status", "unknown")

    @property
    def total_text_chars(self) -> int:
        return int(self._manifest.get("quality_report", {}).get("total_text_chars", 0))

    @property
    def crawl_index(self) -> list[dict[str, Any]]:
        return list(self._manifest.get("crawl_index", []))

    def resource_by_id(self, resource_id: str) -> Resource | None:
        """Look up a single fetched Resource by its `resource-NNNN` id.

        Returns None if the id has no fetched-status entry in the manifest.
        """
        for r in self.fetched_resources():
            if r.resource_id == resource_id:
                return r
        return None

    def fetched_resources(self) -> list[Resource]:
        out: list[Resource] = []
        for entry in self.crawl_index:
            if entry.get("status") != "fetched":
                continue
            resource_id = entry.get("resource_id")
            if not resource_id:
                continue
            number = _resource_number(resource_id)
            out.append(
                Resource(
                    resource_id=resource_id,
                    url=entry["url"],
                    final_url=entry.get("final_url"),
                    page_class=entry.get("page_class", "other"),
                    depth=int(entry.get("depth", 0)),
                    text_path=_first_match(self.corpus_root / "text", f"{number}-*.txt"),
                    markdown_path=_first_match(self.corpus_root / "markdown", f"{number}-*.md"),
                    raw_html_path=_first_match(self.corpus_root / "raw", f"{number}-*.html"),
                    json_path=_first_match(self.corpus_root / "json", f"{number}-*.json"),
                )
            )
        return out

    def skipped_resources(self) -> list[SkippedEntry]:
        out: list[SkippedEntry] = []
        for entry in self.crawl_index:
            status = entry.get("status", "")
            if status == "fetched":
                continue
            out.append(
                SkippedEntry(
                    url=entry["url"],
                    page_class=entry.get("page_class", "other"),
                    status=status,
                    skip_reason=entry.get("skip_reason"),
                    depth=int(entry.get("depth", 0)),
                    discovered_from=tuple(entry.get("discovered_from", []) or ()),
                )
            )
        return out


def _resource_number(resource_id: str) -> str:
    """`resource-0001` -> `0001`."""
    _, _, number = resource_id.partition("-")
    return number


def _first_match(directory: Path, pattern: str) -> Path | None:
    if not directory.is_dir():
        return None
    matches = sorted(directory.glob(pattern))
    return matches[0] if matches else None
