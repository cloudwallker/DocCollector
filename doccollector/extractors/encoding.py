"""Shared text decoding and line-search helpers.

The decoding order is explicit and diagnosable so that extraction and hit
location always share one deterministic decode result instead of each caller
guessing an encoding.
"""
from __future__ import annotations

import codecs

from .base import HitLocation

# Encodings we can name reliably. gb18030 is a superset of gbk/gb2312.
_BINARY_HINT = b"\x00"


def decode_bytes(raw: bytes) -> tuple[str, str | None, str | None]:
    """Decode raw bytes to text.

    Returns ``(text, encoding, error)``. ``error`` is None only when the
    encoding was determined reliably; otherwise it explains the limitation so
    the caller can avoid silently polluting the index.
    """
    if raw.startswith(codecs.BOM_UTF8):
        try:
            return raw.decode("utf-8-sig"), "utf-8-sig", None
        except UnicodeDecodeError as exc:
            return raw.decode("utf-8", "replace"), "utf-8-sig(replace)", f"UTF-8 BOM 解码失败: {exc}"

    if raw.startswith(codecs.BOM_UTF16_BE) or raw.startswith(codecs.BOM_UTF16_LE):
        enc = "utf-16-be" if raw.startswith(codecs.BOM_UTF16_BE) else "utf-16-le"
        try:
            # The 'utf-16' codec consumes the BOM and picks the right order.
            return raw.decode("utf-16"), enc, None
        except UnicodeDecodeError as exc:
            return raw.decode("utf-16", "replace"), f"{enc}(replace)", f"UTF-16 解码失败: {exc}"

    # A NUL byte in the head strongly suggests binary data or BOM-less UTF-16,
    # neither of which is in scope this round. Report instead of indexing junk.
    if _BINARY_HINT in raw[:8192]:
        return "", None, "检测到 NUL 字节，疑似二进制文件或无 BOM 的 UTF-16，本版本不支持"

    try:
        return raw.decode("utf-8"), "utf-8", None
    except UnicodeDecodeError:
        pass

    try:
        return raw.decode("gb18030"), "gb18030", None
    except UnicodeDecodeError:
        pass

    # Best-effort statistical detection for anything else.
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(raw).best()
        if best is not None:
            return str(best), best.encoding, None
    except Exception:
        pass

    try:
        import chardet

        det = chardet.detect(raw)
        enc = det.get("encoding")
        if enc:
            conf = det.get("confidence")
            return (
                raw.decode(enc, "replace"),
                f"{enc}(replace)",
                f"编码不确定(confidence={conf})，按 {enc} 替换解码，正文可能含乱码",
            )
    except Exception:
        pass

    return (
        raw.decode("utf-8", "replace"),
        "utf-8(replace)",
        "无法可靠识别编码，已用 UTF-8 替换模式解码，正文可能含乱码",
    )


def read_text_file(path: str) -> tuple[str, str | None, str | None]:
    """Read and decode a text file. Returns ``(text, encoding, error)``."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        return "", None, f"读取失败: {exc}"
    return decode_bytes(raw)


def search_text_lines(text: str, keyword: str, context: int = 40) -> list[HitLocation]:
    """Case-insensitive literal substring search over lines of ``text``.

    Finds every occurrence (including multiple hits on the same line) and
    preserves line numbering across CRLF/LF/empty lines. The BOM is already
    stripped by the decoder, so line 1 is the first visible line.
    """
    hits: list[HitLocation] = []
    if not keyword or not text:
        return hits

    kw_lower = keyword.lower()
    kw_len = len(keyword)
    for line_no, line in enumerate(text.splitlines(), start=1):
        line_lower = line.lower()
        start = 0
        while True:
            idx = line_lower.find(kw_lower, start)
            if idx == -1:
                break
            ctx_start = max(0, idx - context)
            ctx_end = min(len(line), idx + kw_len + context)
            hits.append(
                HitLocation(
                    location_type="line",
                    location_value=line_no,
                    column=idx + 1,
                    context_before=line[ctx_start:idx],
                    matched_text=line[idx:idx + kw_len],
                    context_after=line[idx + kw_len:ctx_end],
                )
            )
            start = idx + kw_len
    return hits
