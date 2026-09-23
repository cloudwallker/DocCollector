from __future__ import annotations

import os
import subprocess
import threading
import uuid
from datetime import datetime

from PySide6.QtCore import (
    Qt, QThread, Signal, QTimer, QEvent, QDate, QTime, QDateTime, QModelIndex,
)
from PySide6.QtGui import QAction, QColor, QTextCursor, QKeySequence
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QCheckBox, QFileDialog, QProgressBar, QGroupBox, QGridLayout, QListWidget,
    QListWidgetItem, QComboBox, QDoubleSpinBox, QDateEdit, QMessageBox, QSplitter,
    QTableView, QTableWidget, QTableWidgetItem, QHeaderView, QDialog,
    QDialogButtonBox, QPlainTextEdit, QTextEdit, QMenu, QAbstractItemView, QFrame,
    QScrollArea,
)

from ..scanner import DirectoryScanner, SUPPORTED_EXTENSIONS
from ..scanner.scanner import FileEntry
from ..index import IndexDatabase
from ..index.database import normalize_path, is_under
from ..search import SearchEngine
from ..search.engine import SearchFilters, format_location
from ..extractors import get_extractor
from ..collector import FileCollector, CollectMode, ConflictPolicy
from ..collector.file_ops import StructureMode, MovePlan
from ..operations import OperationHistory
from .results_model import (
    ResultsModel, format_size, COL_CHECK, COL_NAME, COL_PATH, COL_TYPE,
    COL_SIZE, COL_MTIME, COL_LOCATION,
)
from .location_worker import LocationWorker

ALL_EXTENSIONS = [".txt", ".md", ".json", ".csv", ".pdf", ".docx"]
MODE_LABELS = [("正文", "content"), ("文件名", "filename"), ("正文或文件名", "both")]
SIZE_UNITS = [("KB", 1024), ("MB", 1024 ** 2), ("GB", 1024 ** 3)]


# ---------------------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------------------

class ScanWorker(QThread):
    progress = Signal(int, int, str)   # current, total, message
    result_ready = Signal(dict)        # business result; NOT QThread.finished
    error = Signal(str)

    def __init__(self, roots: list[dict], db: IndexDatabase):
        super().__init__()
        self.roots = roots
        self.db = db
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        counts = {"discovered": 0, "ok": 0, "no_text": 0, "failed": 0,
                  "removed": 0, "cancelled": False}
        processed: set[str] = set()
        try:
            for root in self.roots:
                if self.cancel_event.is_set():
                    break
                root_path = root["path"]
                exts = root.get("extensions") or set(SUPPORTED_EXTENSIONS)
                scanner = DirectoryScanner(set(exts))
                self.progress.emit(0, 0, f"正在发现文件：已发现 0 个文件 · {root_path}")

                def on_discovery(_root_index, _root_total, directories_seen,
                                 files_found, current_directory):
                    self.progress.emit(
                        0, 0,
                        f"正在发现文件：已发现 {files_found} 个文件，"
                        f"遍历 {directories_seen} 个目录 · {current_directory}",
                    )

                scan_result = scanner.scan(
                    [root_path], cancel_event=self.cancel_event,
                    discovery_callback=on_discovery,
                )
                files = scan_result.files
                scanned_norms: set[str] = set()
                total = len(files)
                for i, entry in enumerate(files):
                    if self.cancel_event.is_set():
                        break
                    norm = normalize_path(entry.path)
                    scanned_norms.add(norm)
                    if norm in processed:
                        continue
                    processed.add(norm)
                    counts["discovered"] += 1
                    content, status, error, encoding, units = "", "skipped", None, None, []
                    extractor = get_extractor(entry.path)
                    if extractor is not None:
                        try:
                            res = extractor.extract()
                            status, error, encoding = res.status, res.error, res.encoding
                            content = res.content if res.status == "ok" else ""
                            units = res.units if res.status == "ok" else []
                        except Exception as exc:  # isolate a single damaged file
                            status, error = "error", str(exc)
                    self.db.upsert_document(
                        path=entry.path, name=entry.name, extension=entry.extension,
                        size=entry.size, modified=entry.modified, content=content,
                        content_status=status, extract_error=error, encoding=encoding,
                        units=units,
                        scan_root=root_path,
                    )
                    if status == "ok":
                        counts["ok"] += 1
                    elif status == "no_text":
                        counts["no_text"] += 1
                    else:
                        counts["failed"] += 1
                    self.progress.emit(i + 1, total, f"正在提取正文：{entry.name}")

                root_norm = normalize_path(root_path)
                fully_scanned = scan_result.root_status.get(root_norm) == "complete"
                if fully_scanned and not self.cancel_event.is_set():
                    counts["removed"] += self.db.remove_missing(scanned_norms, [root_path], set(exts))
                self.db.mark_scan_root_scanned(root_path)
            counts["cancelled"] = self.cancel_event.is_set()
            self.result_ready.emit(counts)
        except Exception as exc:  # noqa: BLE001
            self.error.emit(str(exc))
        finally:
            # Release this worker thread's own SQLite connection (close() only
            # affects the calling thread's connection).
            self.db.close()


class SearchWorker(QThread):
    result_ready = Signal(object, int)  # summary, sequence
    error = Signal(str, int)

    def __init__(self, engine: SearchEngine, keyword: str, filters, mode: str, seq: int):
        super().__init__()
        self.engine = engine
        self.keyword = keyword
        self.filters = filters
        self.mode = mode
        self.seq = seq
        self.cancel_event = threading.Event()
        self.thread_ident = None

    def cancel(self):
        self.cancel_event.set()
        if self.thread_ident is not None:
            try:
                self.engine.db.interrupt_thread(self.thread_ident)
            except Exception:
                pass

    def run(self):
        self.thread_ident = threading.get_ident()
        try:
            summary = self.engine.search(
                self.keyword, self.filters, mode=self.mode, cancel_event=self.cancel_event,
            )
            self.result_ready.emit(summary, self.seq)
        except Exception as exc:  # noqa: BLE001
            if not self.cancel_event.is_set():
                self.error.emit(str(exc), self.seq)
        finally:
            self.engine.db.close()


class CollectWorker(QThread):
    progress = Signal(int, int, str)
    result_ready = Signal(list, str, list)  # records, batch_id, sync_errors
    error = Signal(str)

    def __init__(self, collector: FileCollector, plan: MovePlan, db: IndexDatabase,
                 enabled_roots: list[tuple[str, set]], batch_id: str):
        super().__init__()
        self.collector = collector
        self.plan = plan
        self.db = db
        self.enabled_roots = enabled_roots
        self.batch_id = batch_id
        self.cancel_event = threading.Event()
        self.was_cancelled = False
        self.remaining_count = 0

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        sync_errors: list[str] = []
        try:
            records = self.collector.execute_plan(
                self.plan, progress_callback=self._on_progress,
                cancel_event=self.cancel_event, batch_id=self.batch_id,
            )
            self.remaining_count = len(self.plan.items) + len(self.plan.skipped) - len(records)
            self.was_cancelled = self.cancel_event.is_set() and self.remaining_count > 0
            # Cancellation only stops the remaining files; every record that
            # already succeeded must still get its index synchronized, or a
            # moved file stays searchable at its vanished source path.
            self._sync_index(records, sync_errors)
            self.result_ready.emit(records, self.batch_id, sync_errors)
        except Exception as exc:  # noqa: BLE001
            self.error.emit(str(exc))
        finally:
            self.db.close()

    def _on_progress(self, current, total, path):
        self.progress.emit(current, total, os.path.basename(path))

    def _sync_index(self, records, sync_errors: list[str]):
        """Keep the index truthful after copy/move."""
        for rec in records:
            if rec.status != "success":
                continue
            dest = rec.actual_destination or rec.destination
            dest_norm = normalize_path(dest)
            in_scope_root = None
            for root_norm, exts in self.enabled_roots:
                ext = os.path.splitext(dest)[1].lower()
                if is_under(dest_norm, root_norm) and (not exts or ext in exts):
                    in_scope_root = root_norm
                    break
            try:
                if rec.operation == "move":
                    # The source no longer exists; it must not remain searchable.
                    if in_scope_root is not None:
                        self._move_index(rec.source, dest, in_scope_root)
                    else:
                        self.db.delete_document(rec.source)
                else:  # copy
                    if in_scope_root is not None:
                        self._index_one(dest, in_scope_root)
            except Exception as exc:  # noqa: BLE001 - the file operation succeeded
                sync_errors.append(
                    f"{os.path.basename(dest)}：文件操作成功但索引同步失败：{exc}"
                )

    def _read_doc(self, path: str):
        extractor = get_extractor(path)
        content, status, error, encoding, units = "", "skipped", None, None, []
        if extractor is not None:
            res = extractor.extract()
            status, error, encoding = res.status, res.error, res.encoding
            content = res.content if res.status == "ok" else ""
            units = res.units if res.status == "ok" else []
        return content, status, error, encoding, units

    def _move_index(self, source: str, dest: str, root_norm: str):
        """Repoint the index at the destination with freshly read content."""
        content, status, error, encoding, units = self._read_doc(dest)
        stat = os.stat(dest)
        self.db.move_document(
            source, path=dest, name=os.path.basename(dest),
            extension=os.path.splitext(dest)[1].lower(), size=stat.st_size,
            modified=stat.st_mtime, content=content, content_status=status,
            extract_error=error, encoding=encoding, scan_root=root_norm,
            units=units,
        )

    def _index_one(self, path, root_norm):
        extractor = get_extractor(path)
        if extractor is None:
            return
        try:
            content, status, error, encoding, units = self._read_doc(path)
            stat = os.stat(path)
            self.db.upsert_document(
                path=path, name=os.path.basename(path),
                extension=os.path.splitext(path)[1].lower(), size=stat.st_size,
                modified=stat.st_mtime, content=content,
                content_status=status, extract_error=error, encoding=encoding,
                scan_root=root_norm, units=units,
            )
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------

class ScanDirsDialog(QDialog):
    """Manage scan roots (add/remove/enable) and the index formats.

    Scan scope is stored separately from search filters; changing search types
    never re-indexes. Removing a root deletes its index entries on save.
    """

    def __init__(self, db: IndexDatabase, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("扫描目录管理")
        self.setMinimumSize(560, 420)
        saved_roots = db.get_scan_roots()
        self._original = {r["path"] for r in saved_roots}
        self._saved_formats = {
            normalize_path(r["path"]): set(r["extensions"] or SUPPORTED_EXTENSIONS)
            for r in saved_roots
        }
        selected_formats = (set().union(*self._saved_formats.values())
                            if saved_roots else set(SUPPORTED_EXTENSIONS))
        self._formats_changed = False

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("勾选启用目录；移除的目录将在保存时删除其索引："))

        self.list = QListWidget()
        for root in db.get_scan_roots():
            item = QListWidgetItem(root["path"])
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if root["enabled"] else Qt.Unchecked)
            self.list.addItem(item)
        layout.addWidget(self.list, stretch=1)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("添加目录…")
        add_btn.clicked.connect(self._add)
        remove_btn = QPushButton("移除目录")
        remove_btn.clicked.connect(self._remove)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(remove_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        fmt_box = QGroupBox("索引格式")
        fmt_layout = QHBoxLayout(fmt_box)
        self.fmt_checks: dict[str, QCheckBox] = {}
        for ext in ALL_EXTENSIONS:
            cb = QCheckBox(ext.lstrip(".").upper())
            cb.setChecked(ext in selected_formats)
            cb.toggled.connect(self._on_formats_changed)
            self.fmt_checks[ext] = cb
            fmt_layout.addWidget(cb)
        fmt_layout.addStretch()
        layout.addWidget(fmt_box)
        format_hint = QLabel("未修改格式时保留各目录原配置；修改后统一应用到列表中的目录。")
        format_hint.setWordWrap(True)
        layout.addWidget(format_hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _add(self):
        path = QFileDialog.getExistingDirectory(self, "选择要扫描的目录")
        if not path:
            return
        existing = {self.list.item(i).text() for i in range(self.list.count())}
        if normalize_path(path) in {normalize_path(p) for p in existing}:
            return
        item = QListWidgetItem(path)
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Checked)
        self.list.addItem(item)

    def _remove(self):
        row = self.list.currentRow()
        if row >= 0:
            self.list.takeItem(row)

    def _selected_extensions(self) -> set:
        return {ext for ext, cb in self.fmt_checks.items() if cb.isChecked()}

    def _on_formats_changed(self, _checked):
        self._formats_changed = True

    def _save(self):
        exts = self._selected_extensions()
        if not exts:
            QMessageBox.warning(self, "未选择索引格式", "请至少选择一种格式；如需停止扫描，请取消勾选目录。")
            return
        current: dict[str, bool] = {}
        for i in range(self.list.count()):
            item = self.list.item(i)
            current[item.text()] = item.checkState() == Qt.Checked

        # Roots removed from the list -> delete their index entries.
        current_norms = {normalize_path(p) for p in current}
        for old in self._original:
            if normalize_path(old) not in current_norms:
                self.db.remove_scan_root(old)

        for path, enabled in current.items():
            root_exts = (exts if self._formats_changed
                         else self._saved_formats.get(normalize_path(path), exts))
            self.db.save_scan_root(path, root_exts, enabled)
        self.accept()


class MovePlanDialog(QDialog):
    def __init__(self, plan: MovePlan, parent=None):
        super().__init__(parent)
        action = "复制" if plan.mode == CollectMode.COPY else "移动"
        conflict_label = {
            ConflictPolicy.SKIP: "跳过",
            ConflictPolicy.REPLACE: "替换",
            ConflictPolicy.KEEP_BOTH: "保留两者",
        }[plan.conflict_policy]
        self.setWindowTitle(f"{action}计划预览")
        self.setMinimumSize(680, 460)
        layout = QVBoxLayout(self)

        summary = (
            f"操作：<b>{action}</b> ｜ 结构：{'保留目录' if plan.structure_mode == StructureMode.PRESERVE else '平铺'}"
            f" ｜ 冲突策略：<b>{conflict_label}</b><br>"
            f"将执行 <b>{plan.file_count}</b> 个文件，共 <b>{format_size(plan.total_size)}</b>；"
            f"跳过 <b>{plan.skipped_count}</b> 个。"
        )
        info = QLabel(summary)
        info.setWordWrap(True)
        layout.addWidget(info)

        if plan.mode == CollectMode.MOVE and plan.conflict_policy == ConflictPolicy.REPLACE:
            warn = QLabel("⚠ 移动并替换：目标处已存在的文件将被覆盖且无法恢复，源文件将不再保留。")
            warn.setStyleSheet("color:#b00020;font-weight:bold;")
            warn.setWordWrap(True)
            layout.addWidget(warn)

        roots = plan.source_roots
        layout.addWidget(QLabel(f"来源（{len(roots)} 个目录，最多显示 20）："))
        src_box = QTextEdit()
        src_box.setReadOnly(True)
        src_box.setMaximumHeight(70)
        src_box.setPlainText("\n".join(roots[:20]))
        layout.addWidget(src_box)
        layout.addWidget(QLabel(f"目标目录：{plan.target_dir}"))

        table = QTableWidget()
        table.setColumnCount(4)
        table.setHorizontalHeaderLabels(["源", "目标", "状态", "说明"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        table.horizontalHeader().setStretchLastSection(True)
        rows = [
            (i.source, i.destination, "执行", i.note or ("" if i.conflict == "none" else i.conflict))
            for i in plan.items
        ]
        rows += [(s.source, s.destination, "跳过", s.note) for s in plan.skipped]
        table.setRowCount(min(len(rows), 500))
        for r, (src, dest, kind, note) in enumerate(rows[:500]):
            table.setItem(r, 0, QTableWidgetItem(src))
            table.setItem(r, 1, QTableWidgetItem(dest))
            table.setItem(r, 2, QTableWidgetItem(kind))
            table.setItem(r, 3, QTableWidgetItem(note))
        table.resizeColumnsToContents()
        layout.addWidget(table, stretch=1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText(f"确认{action}")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class PreviewPane(QWidget):
    """Shows every hit of the focused file with coloured highlight.

    Content is rendered as PLAIN TEXT (QPlainTextEdit + extra selections), so
    HTML inside a document is never interpreted as markup or executed.
    """

    open_folder = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        self.header = QLabel("未选择文件")
        self.header.setStyleSheet("font-weight:bold;")
        self.header.setWordWrap(True)
        layout.addWidget(self.header)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        layout.addWidget(self.text, stretch=1)
        nav = QHBoxLayout()
        self.prev_btn = QPushButton("上一处")
        self.next_btn = QPushButton("下一处")
        self.prev_btn.setEnabled(False)
        self.next_btn.setEnabled(False)
        self.prev_btn.clicked.connect(lambda: self._step(-1))
        self.next_btn.clicked.connect(lambda: self._step(1))
        self.open_btn = QPushButton("打开所在文件夹")
        nav.addWidget(self.prev_btn)
        nav.addWidget(self.next_btn)
        nav.addStretch()
        nav.addWidget(self.open_btn)
        layout.addLayout(nav)
        self._ranges: list[tuple[int, int]] = []
        self._cursor = 0
        self._hits = []
        self._page_start = 0
        self._path = ""
        self.open_btn.clicked.connect(lambda: self.open_folder.emit(self._path))

    def clear(self):
        self._path = ""
        self._ranges = []
        self._hits = []
        self._page_start = 0
        self.header.setText("未选择文件")
        self.header.setToolTip("")
        self.text.clear()
        self.prev_btn.setEnabled(False)
        self.next_btn.setEnabled(False)

    def show_file(self, path: str, keyword: str, hits):
        self._path = path
        self._hits = hits
        self._ranges = []
        self._cursor = 0
        self._page_start = 0
        self.header.setText(f"{os.path.basename(path)} ｜ 关键词「{keyword}」 ｜ 命中 {len(hits)} 处")
        self.header.setToolTip(path)
        if not hits:
            self.text.setPlainText("正文中未找到该关键词（可能为文件名命中，或文件在索引后已变化）。")
            self.prev_btn.setEnabled(False)
            self.next_btn.setEnabled(False)
            return
        self._render_page()
        self.prev_btn.setEnabled(len(hits) > 1)
        self.next_btn.setEnabled(len(hits) > 1)

    def _render_page(self):
        self._page_start = (self._cursor // 100) * 100
        self._ranges = []
        parts: list[str] = []
        pos = 0  # QTextCursor positions count UTF-16 code units.
        def utf16_length(value: str) -> int:
            return len(value.encode("utf-16-le")) // 2
        for hit in self._hits[self._page_start:self._page_start + 100]:
            label = f"【{format_location(hit)}】 …"
            before = hit.context_before
            matched = hit.matched_text
            after = hit.context_after
            line = f"{label}{before}{matched}{after}…\n"
            start = pos + utf16_length(label) + utf16_length(before)
            self._ranges.append((start, utf16_length(matched)))
            parts.append(line)
            pos += utf16_length(line)
        self.text.setPlainText("".join(parts))
        self._apply_highlight()
        if self._ranges:
            self._scroll_to(self._cursor - self._page_start)

    def _apply_highlight(self):
        selections = []
        text_length = self.text.document().characterCount() - 1
        for start, length in self._ranges:
            sel = QTextEdit.ExtraSelection()
            cursor = self.text.textCursor()
            cursor.setPosition(min(start, text_length))
            cursor.setPosition(min(start + length, text_length), QTextCursor.KeepAnchor)
            sel.cursor = cursor
            sel.format.setBackground(QColor("#ffe08a"))
            sel.format.setForeground(QColor("#000000"))
            selections.append(sel)
        self.text.setExtraSelections(selections)

    def _step(self, delta: int):
        if not self._hits:
            return
        self._cursor = (self._cursor + delta) % len(self._hits)
        if self._cursor // 100 != self._page_start // 100:
            self._render_page()
        else:
            self._scroll_to(self._cursor - self._page_start)

    def _scroll_to(self, index: int):
        if index >= len(self._ranges):
            return
        start, length = self._ranges[index]
        cursor = self.text.textCursor()
        cursor.setPosition(min(start, self.text.document().characterCount() - 1))
        self.text.setTextCursor(cursor)
        self.text.ensureCursorVisible()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self, db: IndexDatabase | None = None):
        super().__init__()
        self.setWindowTitle("DocCollector")
        self.resize(1000, 680)
        self.setMinimumSize(800, 520)

        self.db = db or IndexDatabase()
        self.engine = SearchEngine(self.db)
        self.history = OperationHistory()
        self.collector = FileCollector(history=self.history)

        self.model = ResultsModel(engine=self.engine)
        # Referenced by eventFilter during UI construction; keep it None-safe.
        self.results_view = None
        self._workers: set[QThread] = set()
        self._search_worker: SearchWorker | None = None
        self._collect_worker: CollectWorker | None = None
        self._scan_worker: ScanWorker | None = None
        self._search_seq = 0
        self._search_request = (0, "", "content")
        self._focus_seq = 0
        self._location_pending: dict[tuple, list[int]] = {}
        self._composing = False
        self._stats_dialog = None
        self._stats_table = None
        self._busy = False
        self._ready = False
        self._closing = False
        self._shutdown_timer = QTimer(self)
        self._shutdown_timer.setInterval(40)
        self._shutdown_timer.timeout.connect(self._finish_close_when_idle)

        self._build_ui()
        self._build_menu()
        self._location_worker = LocationWorker(self.engine)
        self._location_worker.result_ready.connect(self._on_location_ready)
        self.model.location_requested.connect(self._on_location_requested)
        self._location_worker.start()
        self._ready = True
        self._refresh_index_state()

    # -- UI construction --------------------------------------------------

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 12, 16, 10)
        root.setSpacing(10)

        header = QHBoxLayout()
        heading = QVBoxLayout()
        heading.setSpacing(1)
        title = QLabel("DocCollector")
        title.setStyleSheet("font-size: 20px; font-weight: 700; color: #162A47;")
        heading.addWidget(title)
        subtitle = QLabel("本地文档检索与归集")
        subtitle.setProperty("role", "muted")
        heading.addWidget(subtitle)
        header.addLayout(heading)
        header.addStretch(1)
        self.state_label = QLabel("状态：就绪")
        self.state_label.setProperty("role", "status")
        self.state_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.state_label.setWordWrap(True)
        header.addWidget(self.state_label, 2)
        root.addLayout(header)

        # Search row
        search_row = QHBoxLayout()
        self.mode_combo = QComboBox()
        self.mode_combo.setMinimumWidth(126)
        for label, _ in MODE_LABELS:
            self.mode_combo.addItem(label)
        self.mode_combo.currentIndexChanged.connect(self._on_filter_changed)
        search_row.addWidget(self.mode_combo)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("输入关键词…（Enter 立即搜索，Esc 取消）")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._on_search_text_changed)
        self.search_input.returnPressed.connect(self._on_search_return)
        self.search_input.installEventFilter(self)
        search_row.addWidget(self.search_input, stretch=1)

        self.search_btn = QPushButton("搜索")
        self.search_btn.setProperty("variant", "primary")
        self.search_btn.clicked.connect(lambda: self.start_search(immediate=True))
        search_row.addWidget(self.search_btn)

        self.filter_btn = QPushButton("筛选 ▾")
        self.filter_btn.setCheckable(True)
        self.filter_btn.toggled.connect(self._toggle_filter_panel)
        search_row.addWidget(self.filter_btn)
        root.addLayout(search_row)

        # Action row
        action_row = QHBoxLayout()
        self.scan_dirs_btn = QPushButton("扫描目录…")
        self.scan_dirs_btn.clicked.connect(self._open_scan_dirs)
        self.update_index_btn = QPushButton("更新索引")
        self.update_index_btn.clicked.connect(self._on_update_index)
        self.cancel_btn = QPushButton("取消任务")
        self.cancel_btn.setProperty("variant", "danger")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel_active)
        action_row.addWidget(self.scan_dirs_btn)
        action_row.addWidget(self.update_index_btn)
        action_row.addWidget(self.cancel_btn)
        action_row.addStretch()
        root.addLayout(action_row)

        # Progress
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        # Advanced filter panel (collapsed by default)
        self.filter_panel = QScrollArea()
        self.filter_panel.setFrameShape(QFrame.NoFrame)
        self.filter_panel.setWidgetResizable(True)
        self.filter_panel.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.filter_panel.setWidget(self._build_filter_panel())
        self.filter_panel.setMaximumHeight(230)
        self.filter_panel.setVisible(False)
        root.addWidget(self.filter_panel)

        # Side-by-side result list and indexed-content preview.
        self.splitter = QSplitter(Qt.Horizontal)
        result_panel = QWidget()
        result_layout = QVBoxLayout(result_panel)
        result_layout.setContentsMargins(0, 0, 0, 0)
        result_layout.setSpacing(6)
        result_header = QHBoxLayout()
        result_title = QLabel("检索结果")
        result_title.setProperty("role", "sectionTitle")
        result_header.addWidget(result_title)
        result_header.addStretch()
        self.total_label = QLabel("共 0 项 · 0 B")
        self.total_label.setProperty("role", "muted")
        result_header.addWidget(self.total_label)
        self.type_toggle = QPushButton("类型统计 ▾")
        self.type_toggle.setProperty("variant", "quiet")
        self.type_toggle.setCheckable(True)
        self.type_toggle.toggled.connect(self._toggle_type_panel)
        result_header.addWidget(self.type_toggle)
        result_layout.addLayout(result_header)
        self.results_view = QTableView()
        self.results_view.setModel(self.model)
        self.results_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.results_view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.results_view.setSortingEnabled(True)
        self.results_view.setAlternatingRowColors(True)
        self.results_view.verticalHeader().setVisible(False)
        self.results_view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        header = self.results_view.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(True)
        self.results_view.setColumnWidth(COL_CHECK, 36)
        self.results_view.setColumnWidth(COL_NAME, 175)
        self.results_view.setColumnWidth(COL_PATH, 245)
        self.results_view.setColumnWidth(COL_TYPE, 60)
        self.results_view.setColumnWidth(COL_SIZE, 90)
        self.results_view.setColumnWidth(COL_MTIME, 140)
        self.results_view.setColumnHidden(COL_MTIME, True)
        self.results_view.doubleClicked.connect(self._open_current)
        self.results_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.results_view.customContextMenuRequested.connect(self._show_context_menu)
        self.results_view.installEventFilter(self)
        self.results_view.selectionModel().currentChanged.connect(self._on_current_changed)
        result_layout.addWidget(self.results_view, stretch=1)

        self.type_panel = QTableWidget()
        self.type_panel.setColumnCount(3)
        self.type_panel.setHorizontalHeaderLabels(["类型", "数量", "大小"])
        self.type_panel.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.type_panel.setMaximumHeight(140)
        self.type_panel.setVisible(False)
        result_layout.addWidget(self.type_panel)
        self.splitter.addWidget(result_panel)

        preview_panel = QWidget()
        self.preview_panel = preview_panel
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(6)
        preview_title = QLabel("命中预览")
        self.preview_title = preview_title
        preview_title.setProperty("role", "sectionTitle")
        preview_layout.addWidget(preview_title)
        self.preview = PreviewPane()
        self.preview.open_folder.connect(self._open_folder)
        preview_layout.addWidget(self.preview, stretch=1)
        self.splitter.addWidget(preview_panel)
        self.splitter.setStretchFactor(0, 7)
        self.splitter.setStretchFactor(1, 4)
        root.addWidget(self.splitter, stretch=1)

        # Collection controls
        bottom = QHBoxLayout()
        self.checked_label = QLabel("已选 0 项 · 0 B")
        self.checked_label.setStyleSheet("font-weight:bold;")
        bottom.addWidget(self.checked_label)
        bottom.addStretch()

        self.structure_combo = QComboBox()
        self.structure_combo.addItems(["平铺", "保留目录结构"])
        bottom.addWidget(QLabel("结构:"))
        bottom.addWidget(self.structure_combo)
        self.conflict_combo = QComboBox()
        self.conflict_combo.addItems(["跳过", "替换", "保留两者"])
        bottom.addWidget(QLabel("冲突:"))
        bottom.addWidget(self.conflict_combo)

        self.copy_btn = QPushButton("复制到…")
        self.copy_btn.clicked.connect(lambda: self._on_collect(CollectMode.COPY))
        self.move_btn = QPushButton("移动到…")
        self.move_btn.clicked.connect(lambda: self._on_collect(CollectMode.MOVE))
        self.copy_btn.setEnabled(False)
        self.move_btn.setEnabled(False)
        bottom.addWidget(self.copy_btn)
        bottom.addWidget(self.move_btn)
        root.addLayout(bottom)

        self.statusBar().showMessage("就绪")

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(300)
        self._debounce.timeout.connect(lambda: self.start_search(immediate=False))

        self.model.dataChanged.connect(self._update_checked_label)

    def _build_filter_panel(self) -> QWidget:
        panel = QFrame()
        panel.setFrameShape(QFrame.StyledPanel)
        grid = QGridLayout(panel)
        grid.setContentsMargins(6, 6, 6, 6)
        grid.setHorizontalSpacing(4)

        grid.addWidget(QLabel("文件类型："), 0, 0)
        type_row = QHBoxLayout()
        type_row.setSpacing(4)
        self.type_checks: dict[str, QCheckBox] = {}
        for ext in ALL_EXTENSIONS:
            cb = QCheckBox(ext.lstrip(".").upper())
            cb.setChecked(True)
            cb.stateChanged.connect(self._on_filter_changed)
            self.type_checks[ext] = cb
            type_row.addWidget(cb)
        type_row.addStretch()
        grid.addLayout(type_row, 0, 1)

        grid.addWidget(QLabel("最小大小："), 1, 0)
        self.min_size = QDoubleSpinBox()
        self.min_size.setRange(0, 10 ** 9)
        self.min_size.setDecimals(1)
        self.min_unit = QComboBox()
        for label, _ in SIZE_UNITS:
            self.min_unit.addItem(label)
        min_row = QHBoxLayout()
        min_row.addWidget(self.min_size, stretch=1)
        min_row.addWidget(self.min_unit)
        grid.addLayout(min_row, 1, 1)

        grid.addWidget(QLabel("最大大小："), 2, 0)
        self.max_size = QDoubleSpinBox()
        self.max_size.setRange(0, 10 ** 9)
        self.max_size.setDecimals(1)
        self.max_unit = QComboBox()
        for label, _ in SIZE_UNITS:
            self.max_unit.addItem(label)
        self.max_unit.setCurrentIndex(1)
        max_row = QHBoxLayout()
        max_row.addWidget(self.max_size, stretch=1)
        max_row.addWidget(self.max_unit)
        grid.addLayout(max_row, 2, 1)
        self.min_size.valueChanged.connect(self._on_filter_changed)
        self.max_size.valueChanged.connect(self._on_filter_changed)
        self.min_unit.currentIndexChanged.connect(self._on_filter_changed)
        self.max_unit.currentIndexChanged.connect(self._on_filter_changed)

        self.date_enabled = QCheckBox("修改日期范围：")
        self.date_enabled.stateChanged.connect(self._on_filter_changed)
        grid.addWidget(self.date_enabled, 3, 0)
        self.date_from = QDateEdit(QDate.currentDate().addYears(-1))
        self.date_to = QDateEdit(QDate.currentDate())
        for de in (self.date_from, self.date_to):
            de.setCalendarPopup(True)
            de.setDisplayFormat("yyyy-MM-dd")
        grid.addWidget(self.date_from, 3, 1)
        grid.addWidget(QLabel("截止日期："), 4, 0)
        date_to_row = QHBoxLayout()
        date_to_row.addWidget(self.date_to, stretch=1)
        date_to_row.addWidget(QLabel("（含当天）"))
        grid.addLayout(date_to_row, 4, 1)
        self.date_from.dateChanged.connect(self._on_filter_changed)
        self.date_to.dateChanged.connect(self._on_filter_changed)

        grid.addWidget(QLabel("路径包含："), 5, 0)
        self.path_filter = QLineEdit()
        self.path_filter.setPlaceholderText("例如 D:\\资料")
        self.path_filter.textChanged.connect(self._on_filter_changed)
        grid.addWidget(self.path_filter, 5, 1)
        grid.setColumnStretch(1, 1)
        return panel

    def _build_menu(self):
        bar = self.menuBar()
        def add_menu(title):
            menu = QMenu(title, bar)
            bar.addMenu(menu)
            return menu

        m_file = add_menu("文件")
        act_scan_dirs = QAction("扫描目录…", self)
        act_scan_dirs.triggered.connect(self._open_scan_dirs)
        m_file.addAction(act_scan_dirs)
        self.act_scan_dirs = act_scan_dirs
        act_update = QAction("更新索引", self)
        act_update.triggered.connect(self._on_update_index)
        m_file.addAction(act_update)
        m_file.addSeparator()
        act_exit = QAction("退出", self)
        act_exit.triggered.connect(self.close)
        m_file.addAction(act_exit)

        m_edit = add_menu("编辑")
        act_focus = QAction("聚焦搜索", self)
        act_focus.setShortcut(QKeySequence("Ctrl+F"))
        act_focus.triggered.connect(lambda: self.search_input.setFocus())
        m_edit.addAction(act_focus)
        act_check_all = QAction("勾选全部结果", self)
        act_check_all.triggered.connect(self._check_all)
        m_edit.addAction(act_check_all)
        act_clear_check = QAction("清空勾选", self)
        act_clear_check.triggered.connect(self._clear_checked)
        m_edit.addAction(act_clear_check)
        act_check_type = QAction("按类型勾选", self)
        act_check_type.triggered.connect(self._check_by_type)
        m_edit.addAction(act_check_type)

        m_view = add_menu("查看")
        act_preview = QAction("显示/隐藏预览", self)
        act_preview.triggered.connect(
            lambda: self.preview_panel.setVisible(not self.preview_panel.isVisible())
        )
        m_view.addAction(act_preview)
        act_filter = QAction("显示/隐藏筛选", self)
        act_filter.triggered.connect(lambda: self.filter_btn.toggle())
        m_view.addAction(act_filter)
        act_mtime = QAction("显示修改时间列", self)
        act_mtime.setCheckable(True)
        act_mtime.toggled.connect(
            lambda visible: self.results_view.setColumnHidden(COL_MTIME, not visible)
        )
        m_view.addAction(act_mtime)

        m_help = add_menu("帮助")
        act_about = QAction("关于", self)
        act_about.triggered.connect(self._show_about)
        m_help.addAction(act_about)
        # Explicit Qt parent and Python references keep QMenu wrappers valid
        # when QAction wrappers from menuBar().actions() are collected.
        self._menus = [m_file, m_edit, m_view, m_help]

    # -- filters ----------------------------------------------------------

    def _build_filters(self) -> SearchFilters:
        exts = {ext for ext, cb in self.type_checks.items() if cb.isChecked()}
        min_size = None
        if self.min_size.value() > 0:
            min_size = int(self.min_size.value() * SIZE_UNITS[self.min_unit.currentIndex()][1])
        max_size = None
        if self.max_size.value() > 0:
            max_size = int(self.max_size.value() * SIZE_UNITS[self.max_unit.currentIndex()][1])
        after = before_exclusive = None
        if self.date_enabled.isChecked():
            after = QDateTime(self.date_from.date(), QTime(0, 0, 0)).toSecsSinceEpoch()
            before_exclusive = QDateTime(
                self.date_to.date().addDays(1), QTime(0, 0, 0)
            ).toSecsSinceEpoch()
        path_contains = self.path_filter.text().strip() or None
        return SearchFilters(
            extensions=exts, min_size=min_size, max_size=max_size,
            modified_after=after, modified_before_exclusive=before_exclusive,
            path_contains=path_contains,
        )

    def _toggle_filter_panel(self, visible: bool):
        if visible and self.height() < 680 and self.type_toggle.isChecked():
            self.type_toggle.setChecked(False)
        self.filter_panel.setVisible(visible)
        self.filter_btn.setText("筛选 ▴" if visible else "筛选 ▾")

    def _toggle_type_panel(self, visible: bool):
        if visible and self.height() < 680:
            if self.filter_btn.isChecked():
                self.filter_btn.setChecked(False)
            self.type_toggle.setChecked(False)
            self._show_type_stats_dialog()
            return
        self.type_panel.setVisible(visible)
        self.type_toggle.setText("类型统计 ▴" if visible else "类型统计 ▾")

    def _show_type_stats_dialog(self):
        if self._stats_dialog is not None:
            self._copy_type_stats(self._stats_table)
            self._stats_dialog.show()
            self._stats_dialog.raise_()
            self._stats_dialog.activateWindow()
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("类型统计")
        dialog.resize(440, 340)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("当前检索结果的类型分布"))
        table = QTableWidget(0, 3)
        table.setHorizontalHeaderLabels(["类型", "数量", "大小"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._copy_type_stats(table)
        layout.addWidget(table)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.close)
        layout.addWidget(buttons)
        self._stats_dialog = dialog
        self._stats_table = table
        dialog.show()

    def _copy_type_stats(self, table):
        table.setRowCount(self.type_panel.rowCount())
        for row in range(self.type_panel.rowCount()):
            for col in range(3):
                item = self.type_panel.item(row, col)
                table.setItem(row, col, QTableWidgetItem(item.text() if item else ""))

    # -- search -----------------------------------------------------------

    def _on_search_text_changed(self):
        if not self._ready:
            return
        self._invalidate_search()
        # Do not fire meaningless queries while an IME composition is active.
        if self._composing:
            return
        self._debounce.start()

    def _on_search_return(self):
        self._debounce.stop()
        self.start_search(immediate=True)

    def _on_filter_changed(self, *args):
        if self._ready:
            self._invalidate_search()
            self._debounce.start()

    def _invalidate_search(self):
        self._debounce.stop()
        self._search_seq += 1
        self._focus_seq += 1
        self._location_pending.clear()
        self._location_worker.invalidate(self._search_seq)
        if self._search_worker is not None:
            self._search_worker.cancel()
        self.model.clear()
        self.preview.clear()
        self._update_checked_label()

    def _suspend_search_for_task(self):
        """Retire pending search signals before a scan or collection owns the UI."""
        self._debounce.stop()
        self._search_seq += 1
        self._location_pending.clear()
        self._location_worker.invalidate(self._search_seq)
        if self._search_worker is not None:
            self._search_worker.cancel()

    def start_search(self, immediate: bool = False):
        if self._busy:
            self._set_state("后台任务进行中：请等待完成或点击“取消任务”")
            return
        self._debounce.stop()
        self._search_seq += 1
        seq = self._search_seq
        self._location_pending.clear()
        self._location_worker.invalidate(seq)
        keyword = self.search_input.text().strip()  # strip ends only
        mode = MODE_LABELS[self.mode_combo.currentIndex()][1]
        filters = self._build_filters()

        if not self.db.has_documents():
            self.model.set_results([], keyword, mode)
            self.preview.clear()
            self._update_total_label(0, 0, {})
            self._update_checked_label()
            self.progress.setVisible(False)
            if any(root["enabled"] for root in self.db.get_scan_roots()):
                self._set_state("索引为空：请点击“更新索引”；若已更新，请检查目录和索引格式")
            else:
                self._set_state("请先在“扫描目录…”中添加并启用目录，再点击“更新索引”")
            return

        if filters.extensions is not None and len(filters.extensions) == 0:
            self.model.set_results([], keyword, mode)
            self._update_total_label(0, 0, {})
            self._set_state("未选择任何文件类型，结果为空")
            return

        self._set_state("搜索中…")
        self.cancel_btn.setEnabled(True)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        worker = SearchWorker(self.engine, keyword, filters, mode, seq)
        self._search_request = (seq, keyword, mode)
        worker.result_ready.connect(self._on_search_done)
        worker.error.connect(self._on_search_error)
        self._search_worker = worker
        self._register_worker(worker)
        worker.start()

    def _on_search_done(self, summary, seq):
        if self._closing or self._busy:
            return
        if seq != self._search_seq:
            return  # a newer query supersedes this one
        self.progress.setVisible(False)
        self.cancel_btn.setEnabled(self._busy)
        _seq, keyword, mode = self._search_request
        if summary.cancelled:
            self._set_state("已取消")
            return
        self.model.set_results(summary.results, keyword, mode)
        self._update_total_label(summary.total_files, summary.total_size, summary.by_type)
        self.preview.clear()
        if summary.total_files == 0:
            self._set_state("无匹配结果")
        else:
            failed = sum(1 for r in summary.results if r.content_status == "error")
            if failed:
                self._set_state(f"找到 {summary.total_files} 个（其中 {failed} 个解析失败）")
            else:
                self._set_state(f"找到 {summary.total_files} 个结果")
        self._update_checked_label()

    def _on_search_error(self, msg, seq):
        if self._closing or self._busy:
            return
        if seq != self._search_seq:
            return
        self.progress.setVisible(False)
        self.cancel_btn.setEnabled(self._busy)
        self._set_state(f"搜索出错：{msg}")

    # -- results interaction ----------------------------------------------

    def _on_current_changed(self, current: QModelIndex, _previous):
        self._focus_seq += 1
        if not self._ready:
            return
        if not current.isValid():
            self.preview.clear()
            return
        result = self.model.result_at(current.row())
        if result is None:
            self.preview.clear()
            return
        keyword = self.model._keyword
        self.preview._path = result.path
        self.preview.header.setText(f"{result.name} ｜ 正在定位…")
        self.preview.text.clear()
        self._queue_location(result, keyword, self._focus_seq)

    def _on_location_requested(self, path: str, keyword: str, version: str):
        result = next((r for r in self.model._results
                       if r.path == path and r.content_version == version), None)
        if result is not None:
            self._queue_location(result, keyword, 0)

    def _queue_location(self, result, keyword: str, focus: int):
        key = (self._search_seq, result.path, result.content_version, keyword)
        if key in self._location_pending:
            self._location_pending[key].append(focus)
            if focus:
                self._location_worker.submit(*key, result.modified, priority=0)
            return
        self._location_pending[key] = [focus]
        self._location_worker.submit(*key, result.modified,
                                     priority=0 if focus else 1)

    def _on_location_ready(self, seq, path, version, keyword, hits, stale, error):
        key = (seq, path, version, keyword)
        focuses = self._location_pending.pop(key, [])
        if seq != self._search_seq:
            return
        self.model.update_location(path, version, keyword, hits, stale, error)
        if self._focus_seq in focuses and self.preview._path == path:
            self.preview.show_file(path, keyword, hits)
            if error:
                self.preview.header.setText(error)
            elif stale:
                self.preview.header.setText(self.preview.header.text() + " ｜ 索引快照，源文件已变化；请更新索引")

    def _update_checked_label(self, *args):
        count, size = self.model.checked_stats()
        self.checked_label.setText(f"已选 {count} 项 · {format_size(size)}")
        self.copy_btn.setEnabled(count > 0 and not self._busy)
        self.move_btn.setEnabled(count > 0 and not self._busy)

    def _update_total_label(self, total_files, total_size, by_type):
        self.total_label.setText(f"共 {total_files} 项 · {format_size(total_size)}")
        self.type_panel.setRowCount(len(by_type))
        for i, (ext, info) in enumerate(sorted(by_type.items())):
            self.type_panel.setItem(i, 0, QTableWidgetItem(ext.upper().lstrip(".")))
            self.type_panel.setItem(i, 1, QTableWidgetItem(str(info["count"])))
            self.type_panel.setItem(i, 2, QTableWidgetItem(format_size(info["size"])))
        if self._stats_dialog is not None and self._stats_dialog.isVisible():
            self._copy_type_stats(self._stats_table)

    def _check_all(self):
        self.model.check_all()
        self._update_checked_label()

    def _clear_checked(self):
        self.model.clear_checked()
        self._update_checked_label()

    def _check_by_type(self):
        exts = {ext for ext, cb in self.type_checks.items() if cb.isChecked()}
        self.model.check_by_type(exts)
        self._update_checked_label()

    def _open_current(self, index: QModelIndex | None = None):
        if index is None or not index.isValid():
            index = self.results_view.currentIndex()
        result = self.model.result_at(index.row()) if index.isValid() else None
        if result:
            self._open_path(result.path)

    def _open_path(self, path: str):
        try:
            os.startfile(path)  # noqa: S606 - Windows open with default app
        except OSError as exc:
            QMessageBox.warning(self, "无法打开", f"打开文件失败：{exc}")

    def _open_folder(self, path: str):
        if not path:
            return
        try:
            subprocess.Popen(["explorer", "/select," + os.path.normpath(path)])
        except OSError:
            try:
                os.startfile(os.path.dirname(path))  # noqa: S606
            except OSError as exc:
                QMessageBox.warning(self, "无法打开", f"打开文件夹失败：{exc}")

    def _show_context_menu(self, pos):
        index = self.results_view.indexAt(pos)
        if not index.isValid():
            return
        result = self.model.result_at(index.row())
        if result is None:
            return
        menu = QMenu(self)
        act_open = menu.addAction("打开")
        act_folder = menu.addAction("打开所在文件夹")
        act_copy = menu.addAction("复制完整路径")
        menu.addSeparator()
        checked = self.model.is_checked(result.path)
        act_toggle = menu.addAction("取消勾选" if checked else "勾选")
        choice = menu.exec(self.results_view.viewport().mapToGlobal(pos))
        if choice == act_open:
            self._open_path(result.path)
        elif choice == act_folder:
            self._open_folder(result.path)
        elif choice == act_copy:
            from PySide6.QtWidgets import QApplication
            QApplication.clipboard().setText(result.path)
        elif choice == act_toggle:
            self.model.set_checked(result.path, not checked)
            self._update_checked_label()

    # -- event filters (keyboard / IME) -----------------------------------

    def eventFilter(self, obj, event):
        if obj is self.search_input and event.type() == QEvent.InputMethod:
            self._composing = bool(event.preeditString())
        if obj is self.results_view and event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_Space:
                self._toggle_selected_checks()
                return True
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                self._open_current()
                return True
            if event.key() == Qt.Key_Escape:
                self._cancel_active()
                return True
        if obj is self.search_input and event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_Escape:
                self._cancel_active()
                return True
        return super().eventFilter(obj, event)

    def _toggle_selected_checks(self):
        rows = {idx.row() for idx in self.results_view.selectionModel().selectedRows()}
        if not rows:
            current = self.results_view.currentIndex()
            if current.isValid():
                rows = {current.row()}
        paths = [self.model.result_at(r).path for r in rows if self.model.result_at(r)]
        all_checked = paths and all(self.model.is_checked(p) for p in paths)
        for path in paths:
            self.model.set_checked(path, not all_checked)
        if self.model.rowCount():
            top = self.model.index(0, COL_CHECK)
            bottom = self.model.index(self.model.rowCount() - 1, COL_CHECK)
            self.model.dataChanged.emit(top, bottom, [Qt.CheckStateRole])
        self._update_checked_label()

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.Find) or (event.modifiers() & Qt.ControlModifier and event.key() == Qt.Key_F):
            self.search_input.setFocus()
            self.search_input.selectAll()
            event.accept()
            return
        super().keyPressEvent(event)

    # -- scanning ---------------------------------------------------------

    def _open_scan_dirs(self):
        if self._busy:
            QMessageBox.information(self, "正在处理", "请等待当前任务完成后再修改扫描目录。")
            return
        dialog = ScanDirsDialog(self.db, self)
        if dialog.exec():
            self._refresh_index_state()
            self._debounce.start()

    def _on_update_index(self):
        if self._busy:
            QMessageBox.information(self, "正在处理", "请等待当前任务完成。")
            return
        roots = [r for r in self.db.get_scan_roots() if r["enabled"]]
        if not roots:
            QMessageBox.warning(self, "未配置扫描目录", "请先在“扫描目录…”中添加并启用目录。")
            return
        self._suspend_search_for_task()
        self._busy = True
        self.scan_dirs_btn.setEnabled(False)
        self.act_scan_dirs.setEnabled(False)
        self.update_index_btn.setEnabled(False)
        self.search_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self._set_state("索引中…")
        worker = ScanWorker(roots, self.db)
        worker.progress.connect(self._on_scan_progress)
        worker.result_ready.connect(self._on_scan_done)
        worker.error.connect(self._on_scan_error)
        self._scan_worker = worker
        self._register_worker(worker)
        worker.start()

    def _on_scan_progress(self, current, total, message):
        if self._closing:
            return
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(current)
        else:
            self.progress.setRange(0, 0)
        self._set_state(message.split(" · ", 1)[0])
        self.statusBar().showMessage(f"{message}（{current}/{total}）" if total else message)

    def _on_scan_done(self, counts):
        self._busy = False
        if self._closing:
            return
        self.scan_dirs_btn.setEnabled(True)
        self.act_scan_dirs.setEnabled(True)
        self.update_index_btn.setEnabled(True)
        self.search_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.progress.setVisible(False)
        prefix = "索引已取消" if counts.get("cancelled") else "索引完成"
        msg = (
            f"{prefix}：发现 {counts['discovered']}，成功 {counts['ok']}，"
            f"无文本 {counts['no_text']}，失败 {counts['failed']}，清理 {counts['removed']}"
        )
        self._refresh_index_state()
        self._set_state(msg)
        self.statusBar().showMessage(msg)
        if not counts.get("cancelled"):
            self._debounce.start()
        if counts["failed"]:
            self._last_scan_counts = counts

    def _on_scan_error(self, msg):
        self._busy = False
        if self._closing:
            return
        self.scan_dirs_btn.setEnabled(True)
        self.act_scan_dirs.setEnabled(True)
        self.update_index_btn.setEnabled(True)
        self.search_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.progress.setVisible(False)
        self._set_state(f"索引出错：{msg}")
        QMessageBox.critical(self, "索引错误", msg)

    def _refresh_index_state(self):
        stats = self.db.get_stats()
        roots = self.db.get_scan_roots()
        if not roots:
            self._set_state("未建立索引（请先配置扫描目录）")
        elif stats["total_files"] == 0:
            self._set_state("尚未建立索引（点击“更新索引”）")
        else:
            self._set_state(
                f"就绪 ｜ 索引 {stats['total_files']} 个文件 / {format_size(stats['total_size'])}"
            )

    # -- collection -------------------------------------------------------

    def _on_collect(self, mode: CollectMode):
        if self._busy:
            QMessageBox.information(self, "正在处理", "请等待当前任务完成。")
            return
        paths = self.model.checked_paths()
        if not paths:
            QMessageBox.warning(self, "未勾选文件", "请先勾选要归集的文件。")
            return
        target = QFileDialog.getExistingDirectory(self, "选择目标目录")
        if not target:
            return
        structure = StructureMode.PRESERVE if self.structure_combo.currentIndex() == 1 else StructureMode.FLAT
        policy = [ConflictPolicy.SKIP, ConflictPolicy.REPLACE, ConflictPolicy.KEEP_BOTH][
            self.conflict_combo.currentIndex()
        ]
        plan = self.collector.build_plan(paths, target, mode, structure, policy)
        if plan.file_count == 0 and plan.skipped_count == 0:
            QMessageBox.information(self, "无可执行项", "没有可执行的文件。")
            return
        dialog = MovePlanDialog(plan, self)
        if not dialog.exec():
            return

        self._suspend_search_for_task()

        enabled_roots = [
            (normalize_path(r["path"]), set(r["extensions"] or SUPPORTED_EXTENSIONS))
            for r in self.db.get_scan_roots() if r["enabled"]
        ]
        batch_id = uuid.uuid4().hex
        self._busy = True
        self.scan_dirs_btn.setEnabled(False)
        self.act_scan_dirs.setEnabled(False)
        self.copy_btn.setEnabled(False)
        self.move_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.progress.setVisible(True)
        self.progress.setRange(0, plan.file_count + plan.skipped_count)
        self._set_state("正在执行归集…")
        worker = CollectWorker(self.collector, plan, self.db, enabled_roots, batch_id)
        worker.progress.connect(self._on_collect_progress)
        worker.result_ready.connect(self._on_collect_done)
        worker.error.connect(self._on_collect_error)
        self._collect_worker = worker
        self._register_worker(worker)
        worker.start()

    def _on_collect_progress(self, current, total, name):
        self.progress.setValue(current)
        self.statusBar().showMessage(f"处理中 {current}/{total}：{name}")

    def _on_collect_done(self, records, batch_id, sync_errors=None):
        self._busy = False
        if self._closing:
            return
        self.scan_dirs_btn.setEnabled(True)
        self.act_scan_dirs.setEnabled(True)
        self.progress.setVisible(False)
        self.cancel_btn.setEnabled(False)
        success = sum(1 for r in records if r.status == "success")
        skipped = sum(1 for r in records if r.status == "skipped")
        failed = sum(1 for r in records if r.status == "failed")
        history_errors = [r for r in records if r.history_error]
        cancelled = bool(self._collect_worker and self._collect_worker.was_cancelled)
        remaining = self._collect_worker.remaining_count if cancelled else 0
        self._update_checked_label()
        prefix = "操作已取消" if cancelled else "操作完成"
        suffix = f"，未执行 {remaining}" if cancelled else ""
        if history_errors:
            suffix += f"，日志失败 {len(history_errors)}"
        self._set_state(f"{prefix}：成功 {success}，跳过 {skipped}，失败 {failed}{suffix}")
        box = QMessageBox(self)
        box.setWindowTitle("归集结果")
        box.setText(f"{prefix}：成功 {success} 个，跳过 {skipped} 个，失败 {failed} 个{suffix}。")
        if cancelled or history_errors:
            box.setIcon(QMessageBox.Warning)
        detail = "\n".join(
            f"{os.path.basename(r.source)}：{r.error}" for r in records if r.status == "failed"
        )
        if history_errors:
            detail = "\n".join(filter(None, (
                detail,
                *(f"{os.path.basename(r.source)}：日志写入失败：{r.history_error}"
                  for r in history_errors),
            )))
        if sync_errors:
            # The files themselves moved fine; only the index lagged behind.
            detail = "\n".join(filter(None, (detail, *sync_errors)))
            box.setText(box.text() + f"（其中 {len(sync_errors)} 个索引同步失败）")
        if detail:
            box.setDetailedText(detail)
        box.addButton(QMessageBox.Ok)
        box.exec()
        # Refresh search so moved sources disappear from results.
        self._debounce.start()

    def _on_collect_error(self, msg):
        self._busy = False
        if self._closing:
            return
        self.scan_dirs_btn.setEnabled(True)
        self.act_scan_dirs.setEnabled(True)
        self.progress.setVisible(False)
        self.cancel_btn.setEnabled(False)
        self._update_checked_label()
        self._set_state(f"归集出错：{msg}")
        QMessageBox.critical(self, "错误", msg)

    # -- worker lifecycle -------------------------------------------------

    def _register_worker(self, worker: QThread):
        self._workers.add(worker)
        # Retire only on the NATIVE QThread.finished, which fires after run()
        # (including its finally cleanup) has really returned. The business
        # result/error signals do NOT mean the thread is done and must never
        # delete the QThread object underneath it.
        worker.finished.connect(lambda *a, w=worker: self._retire_worker(w))

    def _retire_worker(self, worker: QThread):
        self._workers.discard(worker)
        # Clear only references that still point at THIS worker, so a late
        # old task never drops the newer one that replaced it.
        if self._search_worker is worker:
            self._search_worker = None
        if self._scan_worker is worker:
            self._scan_worker = None
        if self._collect_worker is worker:
            self._collect_worker = None
        worker.deleteLater()

    def _cancel_active(self):
        cancelled = False
        if self._debounce.isActive():
            self._debounce.stop()
            cancelled = True
        self._search_seq += 1
        self._location_pending.clear()
        self._location_worker.invalidate(self._search_seq)
        # Walk the live set instead of the single-slot references: it covers
        # every not-yet-retired worker and tolerates ones whose C++ object
        # has already been released.
        for worker in list(self._workers):
            try:
                if worker.isRunning() and hasattr(worker, "cancel"):
                    worker.cancel()
                    cancelled = True
            except RuntimeError:
                continue
        self.cancel_btn.setEnabled(False)
        if cancelled and self._busy:
            self.progress.setRange(0, 0)
            self.progress.setVisible(True)
            self._set_state("正在取消任务…")
        else:
            self.progress.setVisible(False)
            self._set_state("已请求取消" if cancelled else "就绪")

    def _set_state(self, text: str):
        self.state_label.setText(f"状态：{text}")

    def _show_about(self):
        QMessageBox.about(
            self, "关于 DocCollector",
            "DocCollector 1.1\n\n本地文档正文检索与安全归集工具。\n"
            "支持格式：TXT、MD、JSON、CSV、带文本层的 PDF、DOCX。\n"
            "默认大小写不敏感的字面子串搜索；全部数据本地处理，不上传。",
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "filter_panel"):
            self.filter_panel.setMaximumHeight(60 if self.height() < 680 else 230)
            self.centralWidget().layout().setSpacing(5 if self.height() < 680 else 10)
        if hasattr(self, "preview_title"):
            self.preview_title.setVisible(self.height() >= 680)
        if (self.height() < 680 and hasattr(self, "type_toggle")
                and self.type_toggle.isChecked() and self.filter_btn.isChecked()):
            self.type_toggle.setChecked(False)
        if hasattr(self, "results_view") and self.results_view is not None:
            # At the minimum window size the full path has little readable
            # content; keep the useful name/type/size/location columns in view.
            self.results_view.setColumnHidden(COL_PATH, self.width() < 950)

    # -- shutdown ---------------------------------------------------------

    def closeEvent(self, event):
        self._debounce.stop()
        self._location_worker.cancel()
        live = []
        for worker in list(self._workers):
            try:
                if worker.isRunning():
                    live.append(worker)
                    if hasattr(worker, "cancel"):
                        worker.cancel()
            except RuntimeError:
                continue
        if not live and self._location_worker.isRunning():
            # The queue wake-up normally ends an idle locator immediately.
            # Keep this wait bounded; an active lookup uses deferred close.
            self._location_worker.wait(80)
        if self._location_worker.isRunning() or live:
            event.ignore()
            if not self._closing:
                self._closing = True
                self.centralWidget().setEnabled(False)
                self.progress.setVisible(True)
                self.progress.setRange(0, 0)
                self._set_state("正在停止后台任务并关闭…")
                self.statusBar().showMessage("正在停止后台任务，窗口仍可响应")
                self._shutdown_timer.start()
            return
        self._shutdown_timer.stop()
        stats_dialog = getattr(self, "_stats_dialog", None)
        if stats_dialog is not None:
            stats_dialog.close()
        try:
            self.db.close()
        finally:
            super().closeEvent(event)

    def _finish_close_when_idle(self):
        if self._location_worker.isRunning():
            return
        for worker in list(self._workers):
            try:
                if worker.isRunning():
                    return
            except RuntimeError:
                continue
        self.close()
