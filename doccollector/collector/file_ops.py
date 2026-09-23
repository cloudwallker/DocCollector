from __future__ import annotations

import os
import shutil
import tempfile
from enum import Enum
from dataclasses import dataclass, field

from ..index.database import normalize_path, is_under
from .move_source import locked_move_source


class CollectMode(Enum):
    COPY = "copy"
    MOVE = "move"


class StructureMode(Enum):
    FLAT = "flat"
    PRESERVE = "preserve"


class ConflictPolicy(Enum):
    SKIP = "skip"
    REPLACE = "replace"
    KEEP_BOTH = "keep_both"


@dataclass
class OperationRecord:
    source: str
    destination: str            # planned destination
    actual_destination: str | None = None  # real destination after execution
    operation: str = "copy"     # copy | move
    strategy: str = ""          # conflict policy value
    status: str = "pending"     # success | skipped | failed
    error: str | None = None
    size: int = 0
    history_error: str | None = None


@dataclass
class PlanItem:
    source: str
    destination: str
    size: int = 0
    conflict: str = "none"      # none | renamed | replace | skip_exists | same_file | out_of_scope
    note: str = ""


@dataclass
class MovePlan:
    items: list[PlanItem] = field(default_factory=list)      # actionable
    skipped: list[PlanItem] = field(default_factory=list)    # decided at plan time, not executed
    total_size: int = 0
    mode: CollectMode = CollectMode.COPY
    structure_mode: StructureMode = StructureMode.FLAT
    conflict_policy: ConflictPolicy = ConflictPolicy.SKIP
    target_dir: str = ""

    @property
    def file_count(self) -> int:
        return len(self.items)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)

    @property
    def source_roots(self) -> list[str]:
        roots: set[str] = set()
        for entry in [*self.items, *self.skipped]:
            parent = os.path.dirname(entry.source)
            if parent:
                roots.add(parent)
        return sorted(roots)


def _friendly_error(exc: Exception) -> str:
    if isinstance(exc, PermissionError):
        return f"权限不足或文件被占用：{exc}"
    if isinstance(exc, FileNotFoundError):
        return f"文件不存在：{exc}"
    if isinstance(exc, OSError):
        # ENOSPC(28) disk full, others vary by platform
        if getattr(exc, "errno", None) == 28:
            return f"磁盘空间不足：{exc}"
        return f"操作失败：{exc}"
    return f"操作失败：{exc}"


def _alternate_name(dest: str, counter: int) -> str:
    stem, ext = os.path.splitext(dest)
    return f"{stem} ({counter}){ext}"


def _commit_no_overwrite(tmp_path: str, dest: str) -> None:
    """Publish a staged temp file as ``dest`` atomically, never clobbering.

    A hard link fails with FileExistsError when the target appeared between
    staging and commit, which closes the race that a plain ``exists()``
    re-check cannot. The fallback rename is equally non-overwriting on
    Windows.
    """
    try:
        os.link(tmp_path, dest)
    except FileExistsError:
        raise
    except OSError:
        os.rename(tmp_path, dest)
        return
    os.unlink(tmp_path)


def _same_file(source: str, destination: str) -> bool:
    """Compare aliases as well as spelling (junctions, symlinks, hard links)."""
    if normalize_path(os.path.realpath(source)) == normalize_path(os.path.realpath(destination)):
        return True
    try:
        return os.path.samefile(source, destination)
    except OSError:
        return False


def _root_folder_name(source: str) -> str:
    """Sanitized folder name for a source's drive/UNC root (PRESERVE mode)."""
    drive, _tail = os.path.splitdrive(source)
    if not drive:
        return ""
    if drive.startswith("\\\\"):  # UNC \\server\share
        return drive[2:].replace("\\", "_").replace("/", "_")
    return drive.replace(":", "").replace("\\", "").replace("/", "")


class FileCollector:
    def __init__(self, history=None):
        # ``history`` is an OperationHistory (or any object exposing
        # record_operation(record, batch_id)). Records are persisted per file.
        self.history = history
        self.operations: list[OperationRecord] = []

    # -- planning ---------------------------------------------------------

    def build_plan(
        self,
        file_paths: list[str],
        target_dir: str,
        mode: CollectMode,
        structure_mode: StructureMode,
        conflict_policy: ConflictPolicy,
    ) -> MovePlan:
        plan = MovePlan(
            mode=mode, structure_mode=structure_mode,
            conflict_policy=conflict_policy, target_dir=target_dir,
        )
        target_norm = normalize_path(target_dir)
        reserved: set[str] = set()  # destinations claimed within this plan
        total_size = 0

        for src in file_paths:
            try:
                size = os.path.getsize(src)
            except OSError:
                size = 0

            dest = self._base_destination(src, target_dir, structure_mode)

            # Path-traversal guard: destination must stay inside target_dir.
            if not is_under(normalize_path(dest), target_norm):
                plan.skipped.append(PlanItem(
                    source=src, destination=dest, size=size,
                    conflict="out_of_scope", note="目标路径超出所选目标目录，已拒绝",
                ))
                continue

            # Same-file guard (case/representation-insensitive).
            if _same_file(src, dest):
                plan.skipped.append(PlanItem(
                    source=src, destination=dest, size=size,
                    conflict="same_file", note="源与目标为同一文件，已跳过",
                ))
                continue

            exists_on_disk = os.path.exists(dest)
            reserved_hit = normalize_path(dest) in reserved

            if conflict_policy == ConflictPolicy.KEEP_BOTH:
                if exists_on_disk or reserved_hit:
                    dest = self._unique_destination(dest, reserved)
                    conflict, note = "renamed", "目标已存在，将重命名保留两者"
                else:
                    conflict, note = "none", ""
                reserved.add(normalize_path(dest))
            elif conflict_policy == ConflictPolicy.SKIP:
                if exists_on_disk or reserved_hit:
                    plan.skipped.append(PlanItem(
                        source=src, destination=dest, size=size,
                        conflict="skip_exists", note="目标已存在，按策略跳过",
                    ))
                    continue
                reserved.add(normalize_path(dest))
                conflict, note = "none", ""
            else:  # REPLACE
                if exists_on_disk or reserved_hit:
                    conflict = "replace"
                    note = "将替换已存在文件"
                    if mode == CollectMode.MOVE:
                        note = "移动并替换：目标文件将被覆盖，源文件将不再保留"
                else:
                    conflict, note = "none", ""
                # Reserve the (possibly shared) base name so a second source
                # with the same name does not silently target the same file.
                reserved.add(normalize_path(dest))

            total_size += size
            plan.items.append(PlanItem(
                source=src, destination=dest, size=size, conflict=conflict, note=note,
            ))

        plan.total_size = total_size
        return plan

    def _base_destination(self, src: str, target_dir: str,
                          structure_mode: StructureMode) -> str:
        if structure_mode == StructureMode.FLAT:
            return os.path.join(target_dir, os.path.basename(src))
        drive, tail = os.path.splitdrive(src)
        root_name = _root_folder_name(src)
        rel = tail.lstrip("\\").lstrip("/")
        parts = [target_dir]
        if root_name:
            parts.append(root_name)
        parts.append(rel)
        return os.path.join(*parts)

    @staticmethod
    def _unique_destination(dest: str, reserved: set[str]) -> str:
        """First ``name (n).ext`` that exists neither on disk nor in the plan."""
        if not os.path.exists(dest) and normalize_path(dest) not in reserved:
            return dest
        stem, ext = os.path.splitext(dest)
        counter = 1
        while True:
            candidate = f"{stem} ({counter}){ext}"
            if not os.path.exists(candidate) and normalize_path(candidate) not in reserved:
                return candidate
            counter += 1

    @staticmethod
    def _execute_time_unique(dest: str) -> str:
        """Re-resolve against the live filesystem right before writing.

        Guards Keep Both against a target that appeared after the preview was
        shown, so execution never silently overwrites someone else's file.
        """
        if not os.path.exists(dest):
            return dest
        stem, ext = os.path.splitext(dest)
        counter = 1
        while os.path.exists(f"{stem} ({counter}){ext}"):
            counter += 1
        return f"{stem} ({counter}){ext}"

    # -- execution --------------------------------------------------------

    def execute_plan(self, plan: MovePlan, progress_callback=None,
                     cancel_event=None, batch_id: str | None = None) -> list[OperationRecord]:
        import uuid

        self.operations = []
        batch_id = batch_id or uuid.uuid4().hex
        total = len(plan.skipped) + len(plan.items)

        for index, item in enumerate(plan.skipped):
            record = OperationRecord(
                source=item.source, destination=item.destination,
                actual_destination=item.destination, operation=plan.mode.value,
                strategy=plan.conflict_policy.value, status="skipped",
                error=item.note or item.conflict, size=item.size,
            )
            self._finish(record, batch_id, progress_callback, index, total)

        for index, item in enumerate(plan.items):
            if cancel_event is not None and cancel_event.is_set():
                break

            record = OperationRecord(
                source=item.source, destination=item.destination,
                operation=plan.mode.value, strategy=plan.conflict_policy.value,
                size=item.size,
            )

            try:
                # Re-check same-file at execute time.
                if _same_file(item.source, item.destination):
                    record.status = "skipped"
                    record.error = "源与目标为同一文件，已跳过"
                    record.actual_destination = item.destination
                    self._finish(record, batch_id, progress_callback, index + len(plan.skipped), total)
                    continue

                dest = item.destination
                if plan.conflict_policy == ConflictPolicy.KEEP_BOTH:
                    dest = self._execute_time_unique(dest)
                elif plan.conflict_policy == ConflictPolicy.SKIP and os.path.exists(dest):
                    record.status = "skipped"
                    record.error = "目标已存在，按策略跳过"
                    record.actual_destination = dest
                    self._finish(record, batch_id, progress_callback, index + len(plan.skipped), total)
                    continue

                parent = os.path.dirname(dest)
                if parent:
                    os.makedirs(parent, exist_ok=True)

                final = self._write_atomic(
                    item.source, dest, plan.mode, plan.conflict_policy,
                )
                if final is None:  # target appeared mid-flight under Skip
                    record.status = "skipped"
                    record.error = "目标已存在，按策略跳过"
                    record.actual_destination = dest
                else:
                    record.actual_destination = final
                    record.status = "success"
            except Exception as exc:  # noqa: BLE001 - isolate per-file failures
                record.status = "failed"
                record.error = _friendly_error(exc)
                record.actual_destination = item.destination

            self._finish(record, batch_id, progress_callback, index + len(plan.skipped), total)

        return self.operations

    def _write_atomic(self, src: str, dest: str, mode: CollectMode,
                      policy: ConflictPolicy) -> str | None:
        """Copy/move ``src`` to ``dest`` without ever clobbering a concurrent file.

        The content lands in a temp file inside the destination directory
        first and is published through an exclusive link/rename. A target that
        appeared after planning makes Keep Both pick the next free name and
        Skip back off; only Replace publishes over an existing file. A move
        deletes the source after the destination is fully committed, so a
        failure at any step keeps source and foreign targets intact.
        Returns the final path, or ``None`` when skipped.
        """
        if mode == CollectMode.MOVE:
            with locked_move_source(src) as source:
                return self._publish_atomic(src, dest, policy, source)
        return self._publish_atomic(src, dest, policy)

    def _publish_atomic(self, src: str, dest: str, policy: ConflictPolicy,
                        source=None) -> str | None:
        counter = 0
        candidate = dest
        while True:
            tmp = self._stage_temp(src, candidate, source)
            try:
                if policy == ConflictPolicy.REPLACE:
                    os.replace(tmp, candidate)
                else:
                    _commit_no_overwrite(tmp, candidate)
            except FileExistsError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                if policy == ConflictPolicy.SKIP:
                    return None
                counter += 1
                candidate = _alternate_name(dest, counter)
                continue
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            if source is not None:
                source.delete()
            return candidate

    @staticmethod
    def _stage_temp(src: str, dest: str, source=None) -> str:
        """Copy ``src`` (content + metadata) to a temp file beside ``dest``."""
        stem, _ext = os.path.splitext(os.path.basename(dest))
        fd, tmp = tempfile.mkstemp(
            dir=os.path.dirname(dest) or ".", prefix=f"{stem}.", suffix=".tmp",
        )
        try:
            os.close(fd)
            if source is None:
                shutil.copyfile(src, tmp)
            else:
                source.stream.seek(0)
                with open(tmp, "wb") as output:
                    shutil.copyfileobj(source.stream, output)
            shutil.copystat(src, tmp)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return tmp

    def _finish(self, record: OperationRecord, batch_id: str,
                progress_callback, index: int, total: int) -> None:
        self.operations.append(record)
        # Persist each completed file immediately so a later crash keeps history.
        if self.history is not None:
            try:
                self.history.record_operation(record, batch_id)
            except Exception as exc:
                record.history_error = str(exc)
        if progress_callback:
            progress_callback(index + 1, total, record.source)
