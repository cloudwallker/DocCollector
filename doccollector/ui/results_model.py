from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, QAbstractTableModel, QModelIndex, Signal
from PySide6.QtGui import QColor

from ..search.engine import SearchResult, format_location

COL_CHECK, COL_NAME, COL_PATH, COL_TYPE, COL_SIZE, COL_MTIME, COL_LOCATION = range(7)
HEADERS = ["", "名称", "路径", "类型", "大小", "修改时间", "位置"]


def format_size(size_bytes: int) -> str:
    if size_bytes is None:
        return ""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def format_mtime(ts: float) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError, OverflowError):
        return ""


class ResultsModel(QAbstractTableModel):
    """Table model over the FULL result set.

    Holding every matched row (metadata only) keeps sorting, totals and
    select-all semantics correct across the whole set rather than one page,
    while QTableView avoids creating a widget per row. Hit locations are
    computed lazily for visible rows and cached.
    """

    location_requested = Signal(str, str, str)  # path, keyword, content version

    def __init__(self, engine=None, parent=None):
        super().__init__(parent)
        self._engine = engine
        self._results: list[SearchResult] = []
        self._keyword = ""
        self._mode = "content"
        self._checked: set[str] = set()
        self._loc_cache: dict[str, str] = {}
        self._stale_cache: dict[str, bool] = {}
        self._pending: set[str] = set()
        self._sort_col = COL_NAME
        self._sort_desc = False

    # -- population -------------------------------------------------------

    def set_results(self, results: list[SearchResult], keyword: str, mode: str) -> None:
        self.beginResetModel()
        self._results = list(results)
        self._keyword = keyword
        self._mode = mode
        self._loc_cache.clear()
        self._stale_cache.clear()
        self._pending.clear()
        # A new query/filter clears checks so old results are never collected
        # by accident.
        self._checked.clear()
        self.endResetModel()
        self._apply_sort()

    def clear(self) -> None:
        self.set_results([], "", self._mode)

    def result_at(self, row: int) -> SearchResult | None:
        if 0 <= row < len(self._results):
            return self._results[row]
        return None

    def all_paths(self) -> list[str]:
        return [r.path for r in self._results]

    # -- checks -----------------------------------------------------------

    def is_checked(self, path: str) -> bool:
        return path in self._checked

    def set_checked(self, path: str, checked: bool) -> None:
        if checked:
            self._checked.add(path)
        else:
            self._checked.discard(path)

    def toggle_path(self, path: str) -> bool:
        new = path not in self._checked
        self.set_checked(path, new)
        return new

    def check_all(self) -> None:
        self._checked = {r.path for r in self._results}
        if self._results:
            top = self.index(0, COL_CHECK)
            bottom = self.index(len(self._results) - 1, COL_CHECK)
            self.dataChanged.emit(top, bottom, [Qt.CheckStateRole])

    def clear_checked(self) -> None:
        self._checked.clear()
        if self._results:
            top = self.index(0, COL_CHECK)
            bottom = self.index(len(self._results) - 1, COL_CHECK)
            self.dataChanged.emit(top, bottom, [Qt.CheckStateRole])

    def check_by_type(self, extensions: set[str]) -> None:
        for r in self._results:
            if r.extension in extensions:
                self._checked.add(r.path)
        if self._results:
            top = self.index(0, COL_CHECK)
            bottom = self.index(len(self._results) - 1, COL_CHECK)
            self.dataChanged.emit(top, bottom, [Qt.CheckStateRole])

    def checked_paths(self) -> list[str]:
        order = {r.path: i for i, r in enumerate(self._results)}
        return sorted(self._checked, key=lambda p: order.get(p, 1 << 30))

    def checked_stats(self) -> tuple[int, int]:
        count = 0
        size = 0
        for r in self._results:
            if r.path in self._checked:
                count += 1
                size += r.size
        return count, size

    # -- Qt model API -----------------------------------------------------

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._results)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return HEADERS[section]
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        base = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if index.column() == COL_CHECK:
            base |= Qt.ItemIsUserCheckable
        return base

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        if row >= len(self._results):
            return None
        r = self._results[row]
        col = index.column()

        if col == COL_CHECK:
            if role == Qt.CheckStateRole:
                return Qt.Checked if r.path in self._checked else Qt.Unchecked
            return None

        if role == Qt.ToolTipRole:
            if col in (COL_PATH, COL_NAME):
                tip = r.path
                if r.content_status == "error" and r.extract_error:
                    tip += f"\n解析失败：{r.extract_error}"
                elif r.content_status == "no_text":
                    tip += "\n无可提取文本（本版本不支持 OCR）"
                if self._is_stale(r):
                    tip += "\n注意：文件在索引后已变化，索引可能过期"
                return tip
            return None

        if role == Qt.ForegroundRole:
            if r.content_status in ("error", "no_text") or self._is_stale(r):
                return QColor("#a0a0a0")
            return None

        if role == Qt.UserRole:
            if col == COL_SIZE:
                return r.size
            if col == COL_MTIME:
                return r.modified
            return None

        if role != Qt.DisplayRole:
            return None

        if col == COL_NAME:
            return r.name
        if col == COL_PATH:
            # Qt's table delegate elides long Windows backslash paths down to
            # "C:..." in some styles. Slashes are only a display choice;
            # tooltips and file operations retain the original path.
            return r.path.replace("\\", "/")
        if col == COL_TYPE:
            return r.extension.upper().lstrip(".")
        if col == COL_SIZE:
            return format_size(r.size)
        if col == COL_MTIME:
            return format_mtime(r.modified)
        if col == COL_LOCATION:
            return self._location_label(row, r)
        return None

    def setData(self, index, value, role=Qt.EditRole):
        if not index.isValid() or index.column() != COL_CHECK or role != Qt.CheckStateRole:
            return False
        r = self._results[index.row()]
        # The check delegate passes a raw int (2) on mouse clicks while
        # Qt.Checked is an enum; normalize before comparing or clicks never
        # register.
        try:
            checked = Qt.CheckState(value) == Qt.CheckState.Checked
        except ValueError:
            checked = False
        self.set_checked(r.path, checked)
        self.dataChanged.emit(index, index, [Qt.CheckStateRole])
        return True

    def sort(self, column, order=Qt.AscendingOrder):
        self._sort_col = column
        self._sort_desc = order == Qt.DescendingOrder
        self._apply_sort()

    def _apply_sort(self) -> None:
        if not self._results or self._sort_col == COL_CHECK:
            return
        col = self._sort_col
        def key(r: SearchResult):
            if col == COL_SIZE:
                return r.size
            if col == COL_MTIME:
                return r.modified
            if col == COL_TYPE:
                return r.extension
            if col == COL_PATH:
                return r.path.lower()
            if col == COL_LOCATION:
                # Sort by parse-status bucket; cheap and stable (no row index,
                # no per-row engine lookups). Displayed labels stay richer.
                return {"error": 0, "no_text": 1, "skipped": 2}.get(r.content_status, 3)
            return r.name.lower()
        self.layoutAboutToBeChanged.emit()
        self._results.sort(key=key, reverse=self._sort_desc)
        self.layoutChanged.emit()

    # -- lazy per-row helpers ---------------------------------------------

    def _is_stale(self, r: SearchResult) -> bool:
        self._request_location(r)
        return self._stale_cache.get(r.path, False)

    def _request_location(self, r: SearchResult) -> None:
        if r.path not in self._pending and r.path not in self._stale_cache:
            self._pending.add(r.path)
            self.location_requested.emit(r.path, self._keyword, r.content_version)

    def update_location(self, path: str, version: str, keyword: str,
                        hits, stale: bool, error: str = "") -> None:
        if keyword != self._keyword:
            return
        row = next((i for i, r in enumerate(self._results)
                    if r.path == path and r.content_version == version), None)
        if row is None:
            return
        self._pending.discard(path)
        self._stale_cache[path] = stale
        label = format_location(hits[0]) if hits else ""
        if not label and self._mode in ("filename", "both"):
            label = "文件名"
        self._loc_cache[path] = error or label
        self.dataChanged.emit(self.index(row, COL_NAME), self.index(row, COL_LOCATION))

    def _location_label(self, row: int, r: SearchResult) -> str:
        if r.content_status == "error":
            return "解析失败"
        if r.content_status == "no_text":
            return "无文本"
        if not self._keyword:
            return ""
        if r.path in self._loc_cache:
            return self._loc_cache[r.path]
        self._request_location(r)
        return "文件名" if self._mode == "filename" else "定位中…"
