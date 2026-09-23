from __future__ import annotations

import os

from .base import BaseExtractor, ExtractionResult, HitLocation, ContentUnit
from .encoding import read_text_file, search_text_lines


class TxtExtractor(BaseExtractor):
    """Plain-text extractor shared by TXT/MD/JSON/CSV.

    Extraction and hit location share one cached decode result so they never
    guess different encodings. Subclasses only override ``file_type``.
    """

    file_type = "txt"

    def __init__(self, file_path: str):
        super().__init__(file_path)
        self._cache: tuple[str, str | None, str | None] | None = None

    def _read(self) -> tuple[str, str | None, str | None]:
        if self._cache is None:
            self._cache = read_text_file(self.file_path)
        return self._cache

    def extract(self) -> ExtractionResult:
        try:
            size = os.path.getsize(self.file_path)
        except OSError as exc:
            return ExtractionResult(
                path=self.file_path, file_type=self.file_type, size=0,
                content="", status="error", error=f"无法获取文件大小: {exc}",
            )
        text, encoding, error = self._read()
        status = "error" if error else "ok"
        return ExtractionResult(
            path=self.file_path, file_type=self.file_type, size=size,
            content=text, encoding=encoding, status=status, error=error,
            units=[ContentUnit(line, "line", i) for i, line in
                   enumerate(text.splitlines(), 1)] if status == "ok" else [],
        )

    def search(self, keyword: str) -> list[HitLocation]:
        text, _encoding, _error = self._read()
        return search_text_lines(text, keyword)
