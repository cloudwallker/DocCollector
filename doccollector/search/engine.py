from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from ..index.database import IndexDatabase
from ..extractors.base import HitLocation


@dataclass
class SearchFilters:
    extensions: set[str] | None = None
    min_size: int | None = None
    max_size: int | None = None
    modified_after: float | None = None
    modified_before: float | None = None
    modified_before_exclusive: float | None = None
    path_contains: str | None = None


@dataclass
class SearchResult:
    id: int
    path: str
    name: str
    extension: str
    size: int
    modified: float
    content_status: str = "ok"
    extract_error: str | None = None
    encoding: str | None = None
    indexed_at: float = 0.0
    content_version: str = ""


@dataclass
class SearchSummary:
    results: list[SearchResult]
    total_files: int
    total_size: int
    by_type: dict[str, dict] = field(default_factory=dict)
    cancelled: bool = False


def format_location(hit: HitLocation) -> str:
    """Short human label for a hit, e.g. 行12 / 页3 / 段落2 / 表格1 行2 列3."""
    kind = hit.location_type
    value = hit.location_value
    if kind == "line":
        return f"行{value}"
    if kind == "page":
        return f"页{value}"
    if kind == "paragraph":
        return f"段落{value}"
    return str(value)


class SearchEngine:
    """Literal substring search over the index.

    Candidate matching runs against indexed content in SQLite (filters applied
    before truncation, full-set statistics). Detailed hit positions are loaded
    on demand from the extractor so a search never re-reads every source file.
    """

    def __init__(self, db: IndexDatabase):
        self.db = db

    def search(
        self,
        keyword: str = "",
        filters: SearchFilters | None = None,
        mode: str = "content",
        order_by: str = "name",
        desc: bool = False,
        limit: int | None = None,
        offset: int = 0,
        cancel_event=None,
    ) -> SearchSummary:
        f = filters or SearchFilters()
        raw = self.db.search(
            query=keyword,
            mode=mode,
            extensions=f.extensions,
            min_size=f.min_size,
            max_size=f.max_size,
            modified_after=f.modified_after,
            modified_before=f.modified_before,
            modified_before_exclusive=f.modified_before_exclusive,
            path_contains=f.path_contains,
            order_by=order_by,
            desc=desc,
            limit=limit,
            offset=offset,
            cancel_event=cancel_event,
        )
        if raw.get("cancelled"):
            return SearchSummary(results=[], total_files=0, total_size=0, by_type={}, cancelled=True)

        results = [
            SearchResult(
                id=r["id"], path=r["path"], name=r["name"], extension=r["extension"],
                size=r["size"], modified=r["modified"],
                content_status=r.get("content_status", "ok"),
                extract_error=r.get("extract_error"), encoding=r.get("encoding"),
                indexed_at=r.get("indexed_at", 0.0),
                content_version=r.get("content_version", ""),
            )
            for r in raw["rows"]
        ]
        return SearchSummary(
            results=results,
            total_files=raw["total_files"],
            total_size=raw["total_size"],
            by_type=raw["by_type"],
        )

    def locate(self, path: str, keyword: str, expected_version: str | None = None,
               cancel_event=None) -> list[HitLocation]:
        """Locate in the indexed content snapshot, without opening the source."""
        if not keyword:
            return []
        units = self.db.get_units(path, expected_version)
        hits = []
        pattern = re.compile(re.escape(keyword), re.IGNORECASE | re.ASCII)
        for unit in units:
            if cancel_event is not None and cancel_event.is_set():
                return []
            for match in pattern.finditer(unit.text):
                if cancel_event is not None and cancel_event.is_set():
                    return []
                index, end = match.span()
                hits.append(HitLocation(
                    unit.location_type, unit.location_value,
                    unit.text[max(0, index - 60):index].strip().replace("\n", " "),
                    unit.text[index:end],
                    unit.text[end:end + 60].strip().replace("\n", " "),
                    column=index + 1 if unit.location_type == "line" else None,
                ))
        return hits

    def is_stale(self, path: str, indexed_modified: float) -> bool:
        """True when the file changed or vanished after it was indexed."""
        try:
            return abs(os.path.getmtime(path) - indexed_modified) > 1e-6
        except OSError:
            return True
