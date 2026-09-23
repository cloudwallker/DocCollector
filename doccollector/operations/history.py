from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass, asdict

DEFAULT_HISTORY_PATH = os.path.join(
    os.path.expanduser("~"), ".doccollector", "operation_history.jsonl"
)


@dataclass
class HistoryEntry:
    batch_id: str
    timestamp: float
    source: str
    destination: str
    actual_destination: str | None
    operation: str          # copy | move
    strategy: str           # conflict policy value
    status: str             # success | skipped | failed
    error: str | None = None
    size: int = 0


class OperationHistory:
    """Append-only JSONL operation log.

    Each completed file is flushed to disk immediately so a crash late in a
    batch does not lose the earlier records.
    """

    def __init__(self, history_path: str | None = None):
        self.history_path = history_path or DEFAULT_HISTORY_PATH
        directory = os.path.dirname(self.history_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.entries: list[HistoryEntry] = []
        self._load()

    def _load(self) -> None:
        self.entries = []
        if not os.path.exists(self.history_path):
            return
        try:
            with open(self.history_path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self.entries.append(HistoryEntry(**json.loads(line)))
                    except (json.JSONDecodeError, TypeError):
                        continue
        except OSError:
            self.entries = []

    def record_operation(self, record, batch_id: str) -> HistoryEntry:
        entry = HistoryEntry(
            batch_id=batch_id,
            timestamp=time.time(),
            source=record.source,
            destination=record.destination,
            actual_destination=record.actual_destination,
            operation=record.operation,
            strategy=record.strategy,
            status=record.status,
            error=record.error,
            size=getattr(record, "size", 0),
        )
        self._append(entry)
        self.entries.append(entry)
        return entry

    def record_operations(self, records, batch_id: str | None = None) -> str:
        batch_id = batch_id or uuid.uuid4().hex
        for record in records:
            self.record_operation(record, batch_id)
        return batch_id

    def _append(self, entry: HistoryEntry) -> None:
        with open(self.history_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass

    # -- queries ----------------------------------------------------------

    def batch_ids(self) -> list[str]:
        seen: list[str] = []
        for entry in self.entries:
            if entry.batch_id not in seen:
                seen.append(entry.batch_id)
        return seen

    def get_batch(self, batch_id: str) -> list[HistoryEntry]:
        return [e for e in self.entries if e.batch_id == batch_id]

    def get_last_batch(self) -> list[HistoryEntry]:
        ids = self.batch_ids()
        return self.get_batch(ids[-1]) if ids else []

    def undo_last_move(self) -> list[dict]:
        """Best-effort undo of the most recent MOVE batch only.

        This is intentionally limited and is NOT surfaced as a reliable undo
        because copies are not undone and a Replace that
        overwrote an unbacked-up file cannot be restored.
        """
        results: list[dict] = []
        for entry in reversed(self.get_last_batch()):
            if entry.operation != "move" or entry.status != "success":
                continue
            target = entry.actual_destination or entry.destination
            try:
                if os.path.exists(target):
                    os.makedirs(os.path.dirname(entry.source), exist_ok=True)
                    shutil.move(target, entry.source)
                    results.append({"restored": entry.source, "success": True})
                else:
                    results.append({"source": entry.source, "success": False,
                                    "error": "目标文件不存在"})
            except Exception as exc:  # noqa: BLE001
                results.append({"source": entry.source, "success": False, "error": str(exc)})
        return results
