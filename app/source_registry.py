"""SQLite-backed registry for approved, code-defined holiday sources."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from .sources import SourceError, SourceItem, adapter_types, make_adapter

DEFAULT_DB_PATH = "state/sources.sqlite3"
MAX_SOURCES = 100
MAX_NAME_LENGTH = 80
MAX_URL_LENGTH = 2048
MAX_PARAMS_LENGTH = 4096


@dataclass(frozen=True)
class SourceRecord:
    id: int
    name: str
    adapter_type: str
    url: str
    params: dict[str, Any]
    enabled: bool
    removed: bool
    created_at: str
    updated_at: str
    last_checked_at: str | None
    last_check_ok: bool | None
    last_error: str | None

    @property
    def type(self) -> str:
        return self.adapter_type

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["type"] = self.adapter_type
        return result


class SourceRegistry:
    """Persistent registry with transactional writes and an audit trail."""

    def __init__(self, path: str | Path = DEFAULT_DB_PATH) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        if str(self.path) != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SourceRegistry":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _create_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                adapter_type TEXT NOT NULL,
                url TEXT NOT NULL,
                params_json TEXT NOT NULL DEFAULT '{}',
                enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0, 1)),
                removed INTEGER NOT NULL DEFAULT 0 CHECK(removed IN (0, 1)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_checked_at TEXT,
                last_check_ok INTEGER CHECK(last_check_ok IN (0, 1)),
                last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS source_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER,
                source_name TEXT NOT NULL,
                action TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sources_enabled ON sources(enabled, removed);
            CREATE INDEX IF NOT EXISTS idx_audit_source ON source_audit(source_id, id);
            """
        )

    @staticmethod
    def _now() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    @staticmethod
    def _row(row: sqlite3.Row) -> SourceRecord:
        return SourceRecord(
            id=row["id"],
            name=row["name"],
            adapter_type=row["adapter_type"],
            url=row["url"],
            params=json.loads(row["params_json"]),
            enabled=bool(row["enabled"]),
            removed=bool(row["removed"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_checked_at=row["last_checked_at"],
            last_check_ok=None if row["last_check_ok"] is None else bool(row["last_check_ok"]),
            last_error=row["last_error"],
        )

    def _audit(self, source_id: int | None, name: str, action: str, details: Mapping[str, Any] | None = None) -> None:
        safe_details = {str(k): str(v)[:500] for k, v in (details or {}).items()}
        self._conn.execute(
            "INSERT INTO source_audit(source_id, source_name, action, details_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (source_id, name[:MAX_NAME_LENGTH], action[:40], json.dumps(safe_details, ensure_ascii=False), self._now()),
        )

    def add(
        self,
        name: str,
        adapter_type: str | None = None,
        url: str | None = None,
        params: Mapping[str, Any] | None = None,
        *,
        type: str | None = None,
        adapter: str | None = None,
    ) -> SourceRecord:
        adapter_type = adapter_type or type or adapter
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= MAX_NAME_LENGTH:
            raise SourceError(f"name must be 1-{MAX_NAME_LENGTH} characters")
        name = name.strip()
        if adapter_type not in adapter_types() or adapter_type == "calendru-day":
            # Alias is accepted but canonicalized in storage.
            if adapter_type != "calendru-day":
                raise SourceError(f"unsupported source type: {adapter_type}")
            adapter_type = "calendru_day"
        if not url or len(url) > MAX_URL_LENGTH:
            raise SourceError("url is required and too long")
        values = dict(params or {})
        # Validate through the concrete adapter before persisting.  This keeps
        # adapter-specific URL/path rules in one place and rejects unknown
        # typed parameters at the registry boundary.
        make_adapter(adapter_type, url, values)
        encoded = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded) > MAX_PARAMS_LENGTH:
            raise SourceError("source parameters are too large")
        now = self._now()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            if self._conn.execute("SELECT COUNT(*) FROM sources WHERE removed = 0").fetchone()[0] >= MAX_SOURCES:
                raise SourceError(f"maximum of {MAX_SOURCES} active sources reached")
            cursor = self._conn.execute(
                "INSERT INTO sources(name, adapter_type, url, params_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (name, adapter_type, url, encoded, now, now),
            )
            source_id = int(cursor.lastrowid)
            self._audit(source_id, name, "add", {"adapter_type": adapter_type})
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return self.get(source_id)

    def list(self, *, include_removed: bool = False, enabled_only: bool = False) -> list[SourceRecord]:
        clauses: list[str] = []
        if not include_removed:
            clauses.append("removed = 0")
        if enabled_only:
            clauses.append("enabled = 1")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self._conn.execute(f"SELECT * FROM sources{where} ORDER BY name COLLATE NOCASE").fetchall()
        return [self._row(row) for row in rows]

    def get(self, identifier: int | str) -> SourceRecord:
        if isinstance(identifier, int) or str(identifier).isdigit():
            row = self._conn.execute("SELECT * FROM sources WHERE id = ?", (int(identifier),)).fetchone()
        else:
            row = self._conn.execute("SELECT * FROM sources WHERE name = ? COLLATE NOCASE", (str(identifier),)).fetchone()
        if row is None:
            raise SourceError(f"source not found: {identifier}")
        return self._row(row)

    show = get

    def set_enabled(self, identifier: int | str, enabled: bool) -> SourceRecord:
        record = self.get(identifier)
        if record.removed and enabled:
            raise SourceError("removed source cannot be enabled")
        now = self._now()
        action = "enable" if enabled else "disable"
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute("UPDATE sources SET enabled = ?, updated_at = ? WHERE id = ?", (int(enabled), now, record.id))
            self._audit(record.id, record.name, action)
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return self.get(record.id)

    def enable(self, identifier: int | str) -> SourceRecord:
        return self.set_enabled(identifier, True)

    def disable(self, identifier: int | str) -> SourceRecord:
        return self.set_enabled(identifier, False)

    def remove(self, identifier: int | str, *, confirm: bool = False) -> SourceRecord:
        if not confirm:
            raise SourceError("remove requires confirm=True")
        record = self.get(identifier)
        now = self._now()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute("UPDATE sources SET enabled = 0, removed = 1, updated_at = ? WHERE id = ?", (now, record.id))
            self._audit(record.id, record.name, "remove")
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return self.get(record.id)

    def audit(self, identifier: int | str | None = None) -> list[dict[str, Any]]:
        if identifier is None:
            rows = self._conn.execute("SELECT * FROM source_audit ORDER BY id").fetchall()
        else:
            record = self.get(identifier)
            rows = self._conn.execute("SELECT * FROM source_audit WHERE source_id = ? ORDER BY id", (record.id,)).fetchall()
        return [
            {"id": row["id"], "source_id": row["source_id"], "source_name": row["source_name"], "action": row["action"], "details": json.loads(row["details_json"]), "created_at": row["created_at"]}
            for row in rows
        ]

    def test(self, identifier: int | str, day: str | None = None) -> list[SourceItem]:
        record = self.get(identifier)
        if record.removed:
            raise SourceError("removed source cannot be tested")
        adapter = make_adapter(record.adapter_type, record.url, record.params, source_id=record.name)
        try:
            events = adapter.fetch(day or date.today().isoformat())
        except Exception as exc:
            message = f"{type(exc).__name__}: {str(exc)[:400]}"
            self._record_check(record, False, message)
            raise
        self._record_check(record, True, None)
        return events

    test_source = test

    def _record_check(self, record: SourceRecord, ok: bool, error: str | None) -> None:
        now = self._now()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "UPDATE sources SET last_checked_at = ?, last_check_ok = ?, last_error = ?, updated_at = ? WHERE id = ?",
                (now, int(ok), error, now, record.id),
            )
            self._audit(record.id, record.name, "test_ok" if ok else "test_failed", {"error": error} if error else None)
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def enabled_adapters(self) -> list[tuple[SourceRecord, Any]]:
        return [(record, make_adapter(record.adapter_type, record.url, record.params, source_id=record.name)) for record in self.list(enabled_only=True)]
