from __future__ import annotations

import os

from .base import BaseExtractor, ExtractionResult, HitLocation, ContentUnit


class PdfExtractor(BaseExtractor):
    """Extracts the text layer page by page.

    A PDF without a text layer is reported as ``no_text`` ("无可提取文本，
    本版本不支持 OCR") rather than being counted as a successful index.
    """

    file_type = "pdf"

    def extract(self) -> ExtractionResult:
        try:
            size = os.path.getsize(self.file_path)
        except OSError as exc:
            return ExtractionResult(
                path=self.file_path, file_type="pdf", size=0,
                content="", status="error", error=f"无法获取文件大小: {exc}",
            )
        try:
            import fitz

            doc = fitz.open(self.file_path)
        except Exception as exc:  # noqa: BLE001 - corrupt/unsupported PDF
            return ExtractionResult(
                path=self.file_path, file_type="pdf", size=size,
                content="", status="error", error=f"无法打开 PDF: {exc}",
            )
        try:
            if doc.needs_pass:
                return ExtractionResult(
                    path=self.file_path, file_type="pdf", size=size,
                    content="", status="error", error="PDF 已加密，需要密码，无法提取正文",
                )
            pages = []
            for page in doc:
                pages.append(page.get_text())
            content = "\n".join(pages)
        except Exception as exc:
            return ExtractionResult(
                path=self.file_path, file_type="pdf", size=size,
                content="", status="error", error=f"PDF 解析失败: {exc}",
            )
        finally:
            doc.close()

        if not content.strip():
            return ExtractionResult(
                path=self.file_path, file_type="pdf", size=size,
                content="", status="no_text", error="无可提取文本，本版本不支持 OCR",
            )
        return ExtractionResult(
            path=self.file_path, file_type="pdf", size=size, content=content, status="ok",
            units=[ContentUnit(text, "page", i) for i, text in enumerate(pages, 1)],
        )

    def search(self, keyword: str) -> list[HitLocation]:
        hits: list[HitLocation] = []
        if not keyword:
            return hits
        try:
            import fitz

            doc = fitz.open(self.file_path)
        except Exception:
            return hits

        try:
            kw_lower = keyword.lower()
            kw_len = len(keyword)
            for page_num in range(len(doc)):
                text = doc[page_num].get_text()
                text_lower = text.lower()
                start = 0
                while True:
                    idx = text_lower.find(kw_lower, start)
                    if idx == -1:
                        break
                    ctx_start = max(0, idx - 60)
                    ctx_end = min(len(text), idx + kw_len + 60)
                    hits.append(
                        HitLocation(
                            location_type="page",
                            location_value=page_num + 1,
                            context_before=text[ctx_start:idx].strip().replace("\n", " "),
                            matched_text=text[idx:idx + kw_len],
                            context_after=text[idx + kw_len:ctx_end].strip().replace("\n", " "),
                        )
                    )
                    start = idx + kw_len
        finally:
            doc.close()
        return hits
