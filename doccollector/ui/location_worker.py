"""Single background reader for indexed hit positions and file freshness."""
from __future__ import annotations

import queue
import threading
import itertools

from PySide6.QtCore import QThread, Signal


class LocationWorker(QThread):
    result_ready = Signal(int, str, str, str, object, bool, str)

    def __init__(self, engine):
        super().__init__()
        self.engine = engine
        self._requests = queue.PriorityQueue()
        self._counter = itertools.count()
        self._stop = threading.Event()
        self._active_cancel = threading.Event()
        self._min_seq = 0
        self._thread_ident = None

    def submit(self, seq: int, path: str, version: str, keyword: str,
               modified: float, priority: int = 1) -> None:
        self._requests.put((priority, next(self._counter), seq, path, version, keyword, modified))

    def invalidate(self, seq: int) -> None:
        self._min_seq = seq
        self._active_cancel.set()
        while True:
            try:
                self._requests.get_nowait()
            except queue.Empty:
                break
        if self._thread_ident is not None:
            self.engine.db.interrupt_thread(self._thread_ident)

    def cancel(self) -> None:
        self._stop.set()
        self._active_cancel.set()
        # Wake an idle reader immediately instead of waiting for queue timeout.
        self._requests.put((-1, next(self._counter), -1, "", "", "", 0.0))
        if self._thread_ident is not None:
            self.engine.db.interrupt_thread(self._thread_ident)

    def run(self) -> None:
        self._thread_ident = threading.get_ident()
        try:
            while not self._stop.is_set():
                try:
                    _priority, _order, seq, path, version, keyword, modified = self._requests.get(timeout=.1)
                except queue.Empty:
                    continue
                if self._stop.is_set():
                    break
                if seq < self._min_seq:
                    continue
                self._active_cancel = threading.Event()
                if seq < self._min_seq:
                    continue
                hits, error = [], ""
                try:
                    hits = self.engine.locate(path, keyword, version, self._active_cancel)
                except (KeyError, ValueError) as exc:
                    error = str(exc)
                except Exception as exc:  # keep later requests usable
                    error = f"定位失败：{exc}"
                if self._stop.is_set() or self._active_cancel.is_set() or seq < self._min_seq:
                    continue
                stale = self.engine.is_stale(path, modified)
                if not self._stop.is_set() and seq >= self._min_seq:
                    self.result_ready.emit(seq, path, version, keyword, hits, stale, error)
        finally:
            self.engine.db.close()
