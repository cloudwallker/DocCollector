from __future__ import annotations

import os
from dataclasses import dataclass, field
from time import monotonic

from ..index.database import normalize_path

SUPPORTED_EXTENSIONS = {".txt", ".md", ".json", ".csv", ".pdf", ".docx"}


@dataclass
class FileEntry:
    path: str
    name: str
    extension: str
    size: int
    modified: float
    scan_root: str = ""


@dataclass
class ScanResult:
    """Discovery output plus per-root completeness status.

    ``root_status`` maps a normalized root to one of: complete / missing /
    error / cancelled. Only ``complete`` roots may drive index cleanup, so an
    inaccessible or interrupted scan never deletes still-valid entries.
    """

    files: list[FileEntry] = field(default_factory=list)
    root_status: dict[str, str] = field(default_factory=dict)
    cancelled: bool = False

    def complete_roots(self) -> list[str]:
        return [root for root, status in self.root_status.items() if status == "complete"]


class DirectoryScanner:
    def __init__(self, extensions: set[str] | None = None):
        self.extensions = extensions or SUPPORTED_EXTENSIONS

    def scan(
        self, directories: list[str], progress_callback=None, cancel_event=None,
        discovery_callback=None,
    ) -> ScanResult:
        """Discover supported files in each root.

        ``progress_callback`` retains the root-completion contract
        ``(completed_roots, total_roots, found_files, root_path)``.
        ``discovery_callback`` reports live discovery as
        ``(root_index, total_roots, directories_seen, found_files,
        current_directory)``. Live notifications are limited to five per
        second, with an initial and final notification for each walked root.
        """
        result = ScanResult()
        seen: set[str] = set()

        # De-duplicate roots (case/separator-insensitive), preserving order.
        roots: list[str] = []
        seen_roots: set[str] = set()
        for directory in directories:
            norm = normalize_path(directory)
            if norm in seen_roots:
                continue
            seen_roots.add(norm)
            roots.append(directory)

        total = len(roots)
        directories_seen = 0
        last_discovery_at: float | None = None
        last_discovery_snapshot: tuple | None = None

        def _report_discovery(index: int, current_directory: str, force: bool = False):
            nonlocal last_discovery_at, last_discovery_snapshot
            if discovery_callback is None:
                return
            now = monotonic()
            if not force and last_discovery_at is not None and now - last_discovery_at < 0.2:
                return
            snapshot = (index, total, directories_seen, len(result.files), current_directory)
            if force and snapshot == last_discovery_snapshot:
                return
            discovery_callback(*snapshot)
            last_discovery_at = now
            last_discovery_snapshot = snapshot

        for idx, directory in enumerate(roots):
            norm_root = normalize_path(directory)

            if cancel_event is not None and cancel_event.is_set():
                result.cancelled = True
                for remaining in roots[idx:]:
                    result.root_status.setdefault(normalize_path(remaining), "cancelled")
                break

            if not os.path.isdir(directory):
                result.root_status[norm_root] = "missing"
                if progress_callback:
                    progress_callback(idx + 1, total, len(result.files), directory)
                continue

            errors: list = []

            def _onerror(exc, _errors=errors):
                _errors.append(exc)

            current_directory = directory
            root_directories_seen = 0
            last_discovery_at = None
            last_discovery_snapshot = None
            try:
                for root, _dirs, files in os.walk(directory, onerror=_onerror):
                    if cancel_event is not None and cancel_event.is_set():
                        result.cancelled = True
                        break
                    current_directory = root
                    directories_seen += 1
                    root_directories_seen += 1
                    _report_discovery(idx + 1, root)
                    if cancel_event is not None and cancel_event.is_set():
                        result.cancelled = True
                        break
                    for filename in files:
                        if cancel_event is not None and cancel_event.is_set():
                            result.cancelled = True
                            break
                        ext = os.path.splitext(filename)[1].lower()
                        if ext not in self.extensions:
                            _report_discovery(idx + 1, root)
                            continue
                        full_path = os.path.join(root, filename)
                        file_norm = normalize_path(full_path)
                        if file_norm in seen:  # nested roots must not double count
                            _report_discovery(idx + 1, root)
                            continue
                        try:
                            stat = os.stat(full_path)
                        except OSError as exc:
                            errors.append(exc)
                            _report_discovery(idx + 1, root)
                            continue
                        seen.add(file_norm)
                        result.files.append(
                            FileEntry(
                                path=full_path, name=filename, extension=ext,
                                size=stat.st_size, modified=stat.st_mtime,
                                scan_root=directory,
                            )
                        )
                        _report_discovery(idx + 1, root)
                    if cancel_event is not None and cancel_event.is_set():
                        result.cancelled = True
                    if result.cancelled:
                        break
            except OSError as exc:
                errors.append(exc)

            if root_directories_seen:
                _report_discovery(idx + 1, current_directory, force=True)
            if cancel_event is not None and cancel_event.is_set():
                result.cancelled = True

            if result.cancelled:
                result.root_status[norm_root] = "cancelled"
                for remaining in roots[idx + 1:]:
                    result.root_status.setdefault(normalize_path(remaining), "cancelled")
                break

            result.root_status[norm_root] = "error" if errors else "complete"
            if progress_callback:
                progress_callback(idx + 1, total, len(result.files), directory)

        return result
