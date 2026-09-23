from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class HitLocation:
    """Where a match was found in a document."""

    location_type: str  # "page" | "line" | "paragraph" | "cell"
    location_value: int | str
    context_before: str = ""
    matched_text: str = ""
    context_after: str = ""
    column: int | None = None  # 1-based column within a line, for text files


@dataclass
class ContentUnit:
    text: str
    location_type: str
    location_value: int | str


@dataclass
class ExtractionResult:
    """Unified output from any extractor.

    ``status`` distinguishes the extraction outcomes:
    ``ok`` (text extracted), ``no_text`` (e.g. PDF without a text layer),
    ``error`` (decode/parse failure or unreliable encoding), ``skipped``.
    Only ``ok`` counts as a successful content index.
    """

    path: str
    file_type: str
    size: int
    content: str
    encoding: str | None = None
    status: str = "ok"
    error: str | None = None
    locations: list[HitLocation] = field(default_factory=list)
    units: list[ContentUnit] = field(default_factory=list)


class BaseExtractor(ABC):
    #: subclasses set this to their canonical type label ("txt", "md", ...)
    file_type: str = "txt"

    def __init__(self, file_path: str):
        self.file_path = file_path

    @abstractmethod
    def extract(self) -> ExtractionResult:
        ...

    @abstractmethod
    def search(self, keyword: str) -> list[HitLocation]:
        ...
