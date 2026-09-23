from __future__ import annotations

from .base import BaseExtractor
from .txt import TxtExtractor
from .markdown import MarkdownExtractor
from .json_ext import JsonExtractor
from .pdf import PdfExtractor
from .docx import DocxExtractor

EXTRACTORS = {
    ".txt": TxtExtractor,
    ".md": MarkdownExtractor,
    ".json": JsonExtractor,
    ".csv": TxtExtractor,
    ".pdf": PdfExtractor,
    ".docx": DocxExtractor,
}


def get_extractor(file_path: str) -> BaseExtractor | None:
    import os
    ext = os.path.splitext(file_path)[1].lower()
    cls = EXTRACTORS.get(ext)
    if cls is None:
        return None
    return cls(file_path)
