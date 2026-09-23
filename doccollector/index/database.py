from __future__ import annotations

import os
import sqlite3
import threading
import time
import hashlib
import json

from ..extractors.base import ContentUnit

DEFAULT_DB_PATH = os.path.join(os.path.expanduser("~"), ".doccollector", "index.db")

# Bump when the on-disk schema becomes incompatible. Old indexes are rebuilt
# instead of silently reusing an incompatible index.
SCHEMA_VERSION = 3

_SORTABLE = {
    "name": "d.name",
    "path": "d.path",
    "extension": "d.extension",
    "size": "d.size",
    "modified": "d.modified",
    "indexed_at": "d.indexed_at",
}


def normalize_path(path: str) -> str:
    """Canonical comparison key for a Windows path.

    Folds case, unifies separators and collapses ``.``/``..`` so that paths
    differing only in representation compare equal. UNC roots are preserved by
    normpath/normcase.
    """
    return os.path.normcase(os.path.normpath(path))


def is_under(path_norm: str, root_norm: str) -> bool:
    """True when ``path_norm`` equals or lives inside ``root_norm``."""
    if path_norm == root_norm:
        return True
    return path_norm.startswith(root_norm.rstrip(os.sep) + os.sep)


def escape_like(value: str) -> str:
    """Escape LIKE wildcards so user input is matched literally."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class IndexDatabase:
    """SQLite index with per-thread connections (WAL).

    Each thread that touches the database gets its own connection through
    ``threading.local``; we do NOT share one connection across Qt worker
    threads via ``check_same_thread=False``. WAL plus a busy timeout lets a
    scanning writer and searching readers coexist. ``interrupt`` allows a
    long-running query on a worker connection to be cancelled.
    """

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        directory = os.path.dirname(self.db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._local = threading.local()
        self._locks = threading.Lock()
        self._connections: dict[int, sqlite3.Connection] = {}
        try:
            self._migrate()
        except Exception:
            self.close()
            raise

    # -- connection management -------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=15.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=15000")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
            with self._locks:
                self._connections[threading.get_ident()] = conn
        return conn

    def interrupt(self) -> None:
        """Interrupt any running query on the current thread's connection."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.interrupt()
            except sqlite3.Error:
                pass

    def interrupt_thread(self, thread_ident: int) -> None:
        with self._locks:
            conn = self._connections.get(thread_ident)
        if conn is not None:
            try:
                conn.interrupt()
            except sqlite3.Error:
                pass

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            ident = threading.get_ident()
            with self._locks:
                self._connections.pop(ident, None)
            try:
                conn.close()
            finally:
                self._local.conn = None

    # -- schema -----------------------------------------------------------

    def _migrate(self) -> None:
        conn = self._conn()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            self._create_tables()
            return

        # Fresh or legacy/incompatible schema: preserve configured scan roots,
        # drop everything else and rebuild. Re-indexing is required afterwards.
        conn.execute("BEGIN IMMEDIATE")
        with conn:
            # Another process may have finished the upgrade while this
            # connection waited for the write lock.
            if conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION:
                return
            old_roots: list[tuple[str, str, int]] = []
            try:
                rows = conn.execute("SELECT path, extensions, enabled FROM scan_roots").fetchall()
                old_roots = [(r["path"], r["extensions"], r["enabled"]) for r in rows]
            except sqlite3.OperationalError:
                old_roots = []
            self._drop_legacy()
            self._create_tables()
            for path, exts, enabled in old_roots:
                conn.execute(
                    "INSERT INTO scan_roots(path,norm_path,enabled,extensions) VALUES(?,?,?,?)",
                    (path, normalize_path(path), enabled, exts),
                )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _drop_legacy(self) -> None:
        conn = self._conn()
        for stmt in (
            "DROP TABLE IF EXISTS documents_fts",
            "DROP TABLE IF EXISTS content_units",
            "DROP TABLE IF EXISTS fts_map",
            "DROP TABLE IF EXISTS doc_content",
            "DROP TABLE IF EXISTS documents",
            "DROP TABLE IF EXISTS scan_roots",
        ):
            conn.execute(stmt)

    def _create_tables(self) -> None:
        conn = self._conn()
        for statement in (
            """CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                norm_path TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                extension TEXT NOT NULL,
                size INTEGER NOT NULL,
                modified REAL NOT NULL,
                indexed_at REAL NOT NULL,
                content_status TEXT NOT NULL DEFAULT 'pending',
                extract_error TEXT,
                encoding TEXT,
                scan_root TEXT,
                content_version TEXT NOT NULL DEFAULT ''
            )""",
            "CREATE INDEX IF NOT EXISTS idx_documents_ext ON documents(extension)",
            "CREATE INDEX IF NOT EXISTS idx_documents_root ON documents(scan_root)",
            """CREATE TABLE IF NOT EXISTS content_units (
                doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                location_type TEXT NOT NULL,
                location_value TEXT NOT NULL,
                text TEXT NOT NULL,
                PRIMARY KEY(doc_id, ordinal)
            )""",
            """CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
                content,
                name,
                tokenize='trigram'
            )""",
            """CREATE TABLE IF NOT EXISTS scan_roots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                norm_path TEXT NOT NULL UNIQUE,
                enabled INTEGER NOT NULL DEFAULT 1,
                extensions TEXT NOT NULL DEFAULT '',
                last_scan REAL
            )""",
        ):
            conn.execute(statement)

    # -- writing ----------------------------------------------------------

    def upsert_document(
        self,
        path: str,
        name: str,
        extension: str,
        size: int,
        modified: float,
        content: str = "",
        content_status: str = "ok",
        extract_error: str | None = None,
        encoding: str | None = None,
        scan_root: str | None = None,
        units: list[ContentUnit] | None = None,
    ) -> int:
        conn = self._conn()
        norm = normalize_path(path)
        root_norm = normalize_path(scan_root) if scan_root else None
        now = time.time()
        version = self._content_version(content, units or [])
        with conn:
            row = conn.execute(
                "SELECT id FROM documents WHERE norm_path=?", (norm,)
            ).fetchone()
            if row:
                doc_id = row["id"]
                conn.execute(
                    """UPDATE documents SET path=?, name=?, extension=?, size=?, modified=?,
                           indexed_at=?, content_status=?, extract_error=?, encoding=?,
                           scan_root=?, content_version=? WHERE id=?""",
                    (path, name, extension, size, modified, now, content_status,
                     extract_error, encoding, root_norm, version, doc_id),
                )
            else:
                cur = conn.execute(
                    """INSERT INTO documents
                           (path, norm_path, name, extension, size, modified, indexed_at,
                            content_status, extract_error, encoding, scan_root, content_version)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (path, norm, name, extension, size, modified, now, content_status,
                     extract_error, encoding, root_norm, version),
                )
                doc_id = cur.lastrowid
            self._set_fts(conn, doc_id, content, name)
            self._set_units(conn, doc_id, units or [])
        return doc_id

    @staticmethod
    def _content_version(content: str, units: list[ContentUnit]) -> str:
        data = [content, [IndexDatabase._unit_values(u) for u in units]]
        return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode("utf-8")).hexdigest()

    @staticmethod
    def _unit_values(unit):
        if isinstance(unit, ContentUnit):
            return unit.text, unit.location_type, unit.location_value
        return tuple(unit)

    @staticmethod
    def _set_units(conn: sqlite3.Connection, doc_id: int, units: list[ContentUnit]) -> None:
        conn.execute("DELETE FROM content_units WHERE doc_id=?", (doc_id,))
        for i, unit in enumerate(units):
            text, kind, value = IndexDatabase._unit_values(unit)
            conn.execute(
                "INSERT INTO content_units(doc_id,ordinal,location_type,location_value,text) "
                "VALUES(?,?,?,?,?)", (doc_id, i, kind, value, text),
            )

    def batch_upsert(self, documents: list[dict]) -> None:
        conn = self._conn()
        now = time.time()
        with conn:  # single transaction
            for doc in documents:
                norm = normalize_path(doc["path"])
                root_norm = normalize_path(doc["scan_root"]) if doc.get("scan_root") else None
                units = doc.get("units") or []
                version = self._content_version(doc.get("content", ""), units)
                row = conn.execute(
                    "SELECT id FROM documents WHERE norm_path=?", (norm,)
                ).fetchone()
                if row:
                    doc_id = row["id"]
                    conn.execute(
                        """UPDATE documents SET path=?, name=?, extension=?, size=?, modified=?,
                               indexed_at=?, content_status=?, extract_error=?, encoding=?,
                               scan_root=?, content_version=?
                           WHERE id=?""",
                        (doc["path"], doc["name"], doc["extension"], doc["size"],
                         doc["modified"], now, doc.get("content_status", "ok"),
                         doc.get("extract_error"), doc.get("encoding"), root_norm,
                         version, doc_id),
                    )
                else:
                    cur = conn.execute(
                        """INSERT INTO documents
                               (path, norm_path, name, extension, size, modified, indexed_at,
                                content_status, extract_error, encoding, scan_root, content_version)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (doc["path"], norm, doc["name"], doc["extension"], doc["size"],
                         doc["modified"], now, doc.get("content_status", "ok"),
                         doc.get("extract_error"), doc.get("encoding"), root_norm, version),
                    )
                    doc_id = cur.lastrowid
                self._set_fts(conn, doc_id, doc.get("content", ""), doc["name"])
                self._set_units(conn, doc_id, units)

    def _set_fts(self, conn: sqlite3.Connection, doc_id: int, content: str, name: str) -> None:
        conn.execute("DELETE FROM documents_fts WHERE rowid=?", (doc_id,))
        conn.execute(
            "INSERT INTO documents_fts(rowid, content, name) VALUES (?,?,?)",
            (doc_id, content, name),
        )

    # -- searching --------------------------------------------------------

    def search(
        self,
        query: str = "",
        mode: str = "content",
        extensions: set[str] | None = None,
        min_size: int | None = None,
        max_size: int | None = None,
        modified_after: float | None = None,
        modified_before: float | None = None,
        modified_before_exclusive: float | None = None,
        path_contains: str | None = None,
        order_by: str = "name",
        desc: bool = False,
        limit: int | None = None,
        offset: int = 0,
        cancel_event=None,
    ) -> dict:
        """Literal, case-insensitive substring search with SQL-side filtering.

        Filters are applied BEFORE any truncation; ``total_files``/``total_size``/
        ``by_type`` always describe the full matched set while ``rows`` is just
        the requested page. ``mode`` is one of content/filename/both. An empty
        query browses all indexed documents (filters still apply).
        """
        conn = self._conn()
        where: list[str] = []
        params: list = []

        q = query.strip() if query else ""
        if q:
            # Trigram MATCH produces candidates for long literal phrases. The
            # escaped LIKE below remains the final authority (including %/_).
            # Short terms and embedded NUL use the original full-index scan.
            if len(q) >= 3 and "\x00" not in q:
                where.append(
                    "d.id IN (SELECT rowid FROM documents_fts WHERE documents_fts MATCH ?)"
                )
                params.append('"' + q.replace('"', '""') + '"')
            like = "%" + escape_like(q) + "%"
            # The detailed locator searches within one stored position unit.
            # A phrase spanning two lines/pages/paragraphs is outside the
            # supported location semantics, so do not return it as a hit.
            content_match = (
                "EXISTS (SELECT 1 FROM content_units u WHERE u.doc_id=d.id "
                "AND u.text LIKE ? ESCAPE '\\')"
                if "\n" in q or "\r" in q else "f.content LIKE ? ESCAPE '\\'"
            )
            if mode == "filename":
                where.append("f.name LIKE ? ESCAPE '\\'")
                params.append(like)
            elif mode == "both":
                where.append(f"({content_match} OR f.name LIKE ? ESCAPE '\\')")
                params.extend([like, like])
            else:  # content
                where.append(content_match)
                params.append(like)

        if extensions is not None:
            if len(extensions) == 0:
                # Explicitly no types selected -> empty result set.
                return {"total_files": 0, "total_size": 0, "by_type": {}, "rows": []}
            placeholders = ",".join("?" for _ in extensions)
            where.append(f"d.extension IN ({placeholders})")
            params.extend(sorted(extensions))
        if min_size is not None:
            where.append("d.size >= ?")
            params.append(min_size)
        if max_size is not None:
            where.append("d.size <= ?")
            params.append(max_size)
        if modified_after is not None:
            where.append("d.modified >= ?")
            params.append(modified_after)
        if modified_before is not None:
            where.append("d.modified <= ?")
            params.append(modified_before)
        if modified_before_exclusive is not None:
            where.append("d.modified < ?")
            params.append(modified_before_exclusive)
        if path_contains:
            where.append("d.path LIKE ? ESCAPE '\\'")
            params.append("%" + escape_like(path_contains) + "%")

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        base_from = "FROM documents d JOIN documents_fts f ON f.rowid = d.id"

        def _cancelled() -> bool:
            return cancel_event is not None and cancel_event.is_set()

        total_row = conn.execute(
            f"SELECT COUNT(*) AS c, COALESCE(SUM(d.size),0) AS s {base_from} {where_sql}",
            params,
        ).fetchone()
        if _cancelled():
            return {"total_files": 0, "total_size": 0, "by_type": {}, "rows": [], "cancelled": True}
        total_files = total_row["c"]
        total_size = total_row["s"]

        by_type: dict[str, dict] = {}
        for r in conn.execute(
            f"SELECT d.extension AS ext, COUNT(*) AS c, COALESCE(SUM(d.size),0) AS s "
            f"{base_from} {where_sql} GROUP BY d.extension",
            params,
        ).fetchall():
            by_type[r["ext"]] = {"count": r["c"], "size": r["s"]}
        if _cancelled():
            return {"total_files": 0, "total_size": 0, "by_type": {}, "rows": [], "cancelled": True}

        sort_col = _SORTABLE.get(order_by, "d.name")
        direction = "DESC" if desc else "ASC"
        page_sql = (
            f"SELECT d.id, d.path, d.name, d.extension, d.size, d.modified, "
            f"d.content_status, d.extract_error, d.encoding, d.indexed_at, d.content_version "
            f"{base_from} {where_sql} ORDER BY {sort_col} {direction}"
        )
        page_params = list(params)
        if limit is not None:
            page_sql += " LIMIT ? OFFSET ?"
            page_params.extend([limit, offset])
        rows = [dict(r) for r in conn.execute(page_sql, page_params).fetchall()]

        return {
            "total_files": total_files,
            "total_size": total_size,
            "by_type": by_type,
            "rows": rows,
            "cancelled": False,
        }

    def get_content(self, doc_id: int) -> str:
        row = self._conn().execute(
            "SELECT content FROM documents_fts WHERE rowid=?", (doc_id,)
        ).fetchone()
        return row["content"] if row else ""

    def get_units(self, path: str, expected_version: str | None = None) -> list[ContentUnit]:
        conn = self._conn()
        conn.execute("SAVEPOINT read_units")
        try:
            row = conn.execute("SELECT id, content_version FROM documents WHERE norm_path=?",
                               (normalize_path(path),)).fetchone()
            if row is None:
                raise KeyError(path)
            if expected_version is not None and expected_version != row["content_version"]:
                raise ValueError("索引内容版本已变化，请重新搜索")
            rows = conn.execute(
                "SELECT text, location_type, location_value FROM content_units "
                "WHERE doc_id=? ORDER BY ordinal", (row["id"],)
            ).fetchall()
        finally:
            conn.execute("RELEASE read_units")
        return [ContentUnit(r["text"], r["location_type"],
                            int(r["location_value"]) if r["location_type"] in
                            ("line", "page", "paragraph") else r["location_value"])
                for r in rows]

    def get_document(self, path: str) -> dict | None:
        row = self._conn().execute(
            "SELECT * FROM documents WHERE norm_path=?", (normalize_path(path),)
        ).fetchone()
        return dict(row) if row else None

    def get_stats(self) -> dict:
        conn = self._conn()
        row = conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(size),0) AS s FROM documents"
        ).fetchone()
        by_status = {
            r["content_status"]: r["c"]
            for r in conn.execute(
                "SELECT content_status, COUNT(*) AS c FROM documents GROUP BY content_status"
            ).fetchall()
        }
        by_type = {
            r["extension"]: {"count": r["c"], "size": r["s"]}
            for r in conn.execute(
                "SELECT extension, COUNT(*) AS c, COALESCE(SUM(size),0) AS s "
                "FROM documents GROUP BY extension"
            ).fetchall()
        }
        return {
            "total_files": row["c"],
            "total_size": row["s"],
            "by_status": by_status,
            "by_type": by_type,
        }

    # -- cleanup / scan roots ---------------------------------------------

    def remove_missing(
        self,
        scanned_norm_paths: set[str],
        roots: list[str],
        extensions: set[str],
    ) -> int:
        """Delete index entries only within the fully-scanned scope.

        A document is removed only when it lives under one of ``roots`` AND has
        one of ``extensions`` AND was not discovered in this scan. Documents
        outside the scanned roots or of unscanned types are preserved.
        """
        if not roots or not extensions:
            return 0
        conn = self._conn()
        roots_norm = [normalize_path(r) for r in roots]
        placeholders = ",".join("?" for _ in extensions)
        rows = conn.execute(
            f"SELECT id, norm_path FROM documents WHERE extension IN ({placeholders})",
            sorted(extensions),
        ).fetchall()
        removed = 0
        with conn:
            for r in rows:
                norm = r["norm_path"]
                if norm in scanned_norm_paths:
                    continue
                if any(is_under(norm, root) for root in roots_norm):
                    conn.execute("DELETE FROM documents_fts WHERE rowid=?", (r["id"],))
                    conn.execute("DELETE FROM documents WHERE id=?", (r["id"],))
                    removed += 1
        return removed

    def save_scan_root(self, path: str, extensions: set[str] | None = None,
                       enabled: bool = True) -> None:
        conn = self._conn()
        norm = normalize_path(path)
        exts = ",".join(sorted(extensions)) if extensions else ""
        with conn:
            conn.execute(
                """INSERT INTO scan_roots (path, norm_path, enabled, extensions, last_scan)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(norm_path) DO UPDATE SET
                       path=excluded.path,
                       enabled=excluded.enabled,
                       extensions=CASE WHEN excluded.extensions='' THEN scan_roots.extensions
                                       ELSE excluded.extensions END,
                       last_scan=excluded.last_scan""",
                (path, norm, 1 if enabled else 0, exts, time.time()),
            )

    def mark_scan_root_scanned(self, path: str) -> None:
        """Record a scan without overwriting settings edited while it ran."""
        conn = self._conn()
        with conn:
            conn.execute(
                "UPDATE scan_roots SET last_scan=? WHERE norm_path=?",
                (time.time(), normalize_path(path)),
            )

    def get_scan_roots(self) -> list[dict]:
        rows = self._conn().execute(
            "SELECT path, norm_path, enabled, extensions, last_scan FROM scan_roots ORDER BY path"
        ).fetchall()
        return [
            {
                "path": r["path"],
                "norm_path": r["norm_path"],
                "enabled": bool(r["enabled"]),
                "extensions": set(r["extensions"].split(",")) if r["extensions"] else set(),
                "last_scan": r["last_scan"],
            }
            for r in rows
        ]

    def has_documents(self) -> bool:
        return self._conn().execute("SELECT 1 FROM documents LIMIT 1").fetchone() is not None

    def set_root_enabled(self, path: str, enabled: bool) -> None:
        conn = self._conn()
        with conn:
            conn.execute(
                "UPDATE scan_roots SET enabled=? WHERE norm_path=?",
                (1 if enabled else 0, normalize_path(path)),
            )

    def remove_scan_root(self, path: str) -> int:
        """Remove a configured root and delete its indexed documents."""
        conn = self._conn()
        norm = normalize_path(path)
        rows = conn.execute("SELECT id, norm_path FROM documents").fetchall()
        removed = 0
        with conn:
            for r in rows:
                if is_under(r["norm_path"], norm):
                    conn.execute("DELETE FROM documents_fts WHERE rowid=?", (r["id"],))
                    conn.execute("DELETE FROM documents WHERE id=?", (r["id"],))
                    removed += 1
            conn.execute("DELETE FROM scan_roots WHERE norm_path=?", (norm,))
        return removed

    # -- move synchronization ---------------------------------------------

    def rename_document(self, old_path: str, new_path: str) -> bool:
        """Update a document's path after a move that stays in scope."""
        conn = self._conn()
        old_norm = normalize_path(old_path)
        new_norm = normalize_path(new_path)
        row = conn.execute(
            "SELECT id FROM documents WHERE norm_path=?", (old_norm,)
        ).fetchone()
        if not row:
            return False
        doc_id = row["id"]
        try:
            stat = os.stat(new_path)
            size, modified = stat.st_size, stat.st_mtime
        except OSError:
            size, modified = None, None
        with conn:
            if size is not None:
                conn.execute(
                    "UPDATE documents SET path=?, norm_path=?, size=?, modified=?, indexed_at=? WHERE id=?",
                    (new_path, new_norm, size, modified, time.time(), doc_id),
                )
            else:
                conn.execute(
                    "UPDATE documents SET path=?, norm_path=? WHERE id=?",
                    (new_path, new_norm, doc_id),
                )
            conn.execute(
                "UPDATE documents_fts SET name=? WHERE rowid=?",
                (os.path.basename(new_path), doc_id),
            )
        return True

    def move_document(self, old_path: str, path: str, name: str, extension: str,
                      size: int, modified: float, content: str = "",
                      content_status: str = "ok", extract_error: str | None = None,
                      encoding: str | None = None,
                       scan_root: str | None = None,
                       units: list[ContentUnit] | None = None) -> int:
        """Repoint a moved file's index in one transaction.

        Drops the old source row and inserts-or-replaces the destination row
        with fresh metadata and content, so an in-scope move never loses the
        entry and a pre-existing destination record never keeps its old body.
        """
        conn = self._conn()
        old_norm = normalize_path(old_path)
        norm = normalize_path(path)
        root_norm = normalize_path(scan_root) if scan_root else None
        now = time.time()
        version = self._content_version(content, units or [])
        with conn:  # single transaction
            if old_norm != norm:
                old = conn.execute(
                    "SELECT id FROM documents WHERE norm_path=?", (old_norm,)
                ).fetchone()
                if old:
                    conn.execute("DELETE FROM documents_fts WHERE rowid=?", (old["id"],))
                    conn.execute("DELETE FROM documents WHERE id=?", (old["id"],))
            row = conn.execute(
                "SELECT id FROM documents WHERE norm_path=?", (norm,)
            ).fetchone()
            if row:
                doc_id = row["id"]
                conn.execute(
                    """UPDATE documents SET path=?, name=?, extension=?, size=?, modified=?,
                            indexed_at=?, content_status=?, extract_error=?, encoding=?,
                            scan_root=?, content_version=?
                       WHERE id=?""",
                    (path, name, extension, size, modified, now, content_status,
                      extract_error, encoding, root_norm, version, doc_id),
                )
            else:
                cur = conn.execute(
                    """INSERT INTO documents
                           (path, norm_path, name, extension, size, modified, indexed_at,
                             content_status, extract_error, encoding, scan_root, content_version)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (path, norm, name, extension, size, modified, now, content_status,
                      extract_error, encoding, root_norm, version),
                )
                doc_id = cur.lastrowid
            self._set_fts(conn, doc_id, content, name)
            self._set_units(conn, doc_id, units or [])
        return doc_id

    def delete_document(self, path: str) -> bool:
        conn = self._conn()
        norm = normalize_path(path)
        row = conn.execute(
            "SELECT id FROM documents WHERE norm_path=?", (norm,)
        ).fetchone()
        if not row:
            return False
        with conn:
            conn.execute("DELETE FROM documents_fts WHERE rowid=?", (row["id"],))
            conn.execute("DELETE FROM documents WHERE id=?", (row["id"],))
        return True
