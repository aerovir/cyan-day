"""SQLite content cards, provenance, tags, slots, and publication ledger."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

DEFAULT_CONTENT_DB = "state/content.sqlite3"
STATUSES = frozenset({"verified", "unverified", "disputed", "refuted", "rejected"})
LABELS = {"verified": "ФАКТ", "unverified": "МИФ", "disputed": "РАЗБОР", "refuted": "РАЗБОР", "rejected": "МИФ"}
SOURCE_TYPES = frozenset({"academic", "primary", "museum", "popular", "manual"})
TRUSTED_SOURCE_TYPES = frozenset({"academic", "primary"})
# (slot_key, local_time, requirements, strict): strict-слоты не имеют
# запасного пула — факт не выйдет вечером, миф не выйдет утром.
SLOT_DEFINITIONS = (
    ("fact", "09:00", ("status.verified",), True),
    ("drink", "10:30", ("drink.beer", "drink.wine", "drink.vodka", "topic.production"), False),
    ("context", "12:00", ("topic.context", "topic.history", "topic.people", "topic.places"), False),
    ("custom_law", "13:30", ("topic.customs", "topic.law", "topic.regulation"), False),
    ("trade", "15:00", ("topic.trade", "topic.production"), False),
    ("myth", "17:00", ("format.myth", "status.unverified", "status.rejected"), True),
    ("analysis", "19:00", ("format.analysis", "status.disputed", "status.refuted"), True),
)
TAG_GROUPS = {"era", "drink", "geo", "topic", "format", "tone", "status"}
ANY_DAY = "00-00"
PLAN_VERSION = 2


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _validate_post_id(post_id: int | None) -> int:
    if isinstance(post_id, bool) or not isinstance(post_id, int) or post_id <= 0:
        raise ContentError("published records need a positive VK post id")
    return post_id


def _validate_publication_state(state: str) -> None:
    if state not in {"publishing", "published", "failed", "unknown"}:
        raise ContentError(f"unsupported publication state: {state}")


def _hash_card(card: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(dict(card), ensure_ascii=False, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class ContentCard:
    card_id: str
    revision: int
    calendar_day: str
    title: str
    summary: str
    status: str
    tags: tuple[str, ...]
    claims: tuple[dict[str, Any], ...]
    provenance: tuple[dict[str, Any], ...]
    priority: int = 50
    active: bool = True
    editorial_label: str = ""
    content_hash: str = ""
    image_url: str = ""

    @property
    def label(self) -> str:
        return self.editorial_label or LABELS[self.status]

    @property
    def requires_mistral(self) -> bool:
        return self.status != "verified"

    def packet(self) -> dict[str, Any]:
        return {"card_id": self.card_id, "revision": self.revision, "title": self.title, "status": self.status, "editorial_label": self.label, "summary": self.summary, "claims": list(self.claims), "provenance": list(self.provenance), "tags": list(self.tags)}


class ContentError(ValueError):
    """Invalid editorial content or operation."""


def _check_verified_sources(status: str, sources: Iterable[Mapping[str, Any]]) -> None:
    """verified-карточка обязана иметь хотя бы один доверенный источник."""
    if status == "verified" and not any(source.get("source_type") in TRUSTED_SOURCE_TYPES for source in sources):
        raise ContentError("verified cards need at least one trusted source (academic/primary)")


class ContentStore:
    def __init__(self, path: str | Path = DEFAULT_CONTENT_DB) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "ContentStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _migrate(self) -> None:
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS content_cards(card_id TEXT NOT NULL, revision INTEGER NOT NULL, calendar_day TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('verified','unverified','disputed','refuted','rejected')), editorial_label TEXT NOT NULL DEFAULT '', tags_json TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 50, active INTEGER NOT NULL DEFAULT 1, content_hash TEXT NOT NULL, image_url TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(card_id, revision));
        CREATE TABLE IF NOT EXISTS content_claims(claim_id TEXT PRIMARY KEY, card_id TEXT NOT NULL, revision INTEGER NOT NULL, ordinal INTEGER NOT NULL, claim_text TEXT NOT NULL, provenance_id TEXT NOT NULL DEFAULT '', FOREIGN KEY(card_id,revision) REFERENCES content_cards(card_id,revision) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS provenance_sources(provenance_id TEXT PRIMARY KEY, source_type TEXT NOT NULL, title TEXT NOT NULL, author TEXT NOT NULL DEFAULT '', publication_year INTEGER, url TEXT NOT NULL DEFAULT '', locator TEXT NOT NULL DEFAULT '', quote TEXT NOT NULL DEFAULT '', source_hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS slot_definitions(slot_key TEXT PRIMARY KEY, ordinal INTEGER UNIQUE NOT NULL, local_time TEXT NOT NULL, requirements_json TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS publication_plan(local_date TEXT NOT NULL, slot_key TEXT NOT NULL, card_id TEXT, revision INTEGER, score REAL NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'planned', plan_version INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(local_date,slot_key));
        CREATE TABLE IF NOT EXISTS publication_ledger(idempotency_key TEXT PRIMARY KEY, local_date TEXT NOT NULL, slot_key TEXT NOT NULL, card_id TEXT NOT NULL, revision INTEGER NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, vk_post_id INTEGER, generated_text TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(local_date,slot_key));
        CREATE INDEX IF NOT EXISTS idx_cards_day ON content_cards(calendar_day,active,status);
        CREATE INDEX IF NOT EXISTS idx_ledger_card ON publication_ledger(card_id,local_date);
        """)
        # Полный sync: сетка слотов — код-конфиг, устаревшие строки удаляются
        self.conn.execute("DELETE FROM slot_definitions")
        self.conn.executemany(
            "INSERT INTO slot_definitions(slot_key,ordinal,local_time,requirements_json,enabled) VALUES(?,?,?,?,1)",
            [(key, ordinal, local_time, json.dumps(requirements)) for ordinal, (key, local_time, requirements, _strict) in enumerate(SLOT_DEFINITIONS, 1)],
        )
        self.conn.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(1,?)", (_now(),))
        # v2: image_url для карточек — ALTER только для баз, созданных в v1
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(content_cards)")}
        if "image_url" not in columns:
            self.conn.execute("ALTER TABLE content_cards ADD COLUMN image_url TEXT NOT NULL DEFAULT ''")
        self.conn.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(2,?)", (_now(),))
        # v3: новая сетка слотов (fact/контекст, strict-слоты) — сид выше уже синхронизирован
        self.conn.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(3,?)", (_now(),))

    @staticmethod
    def _validate(data: Mapping[str, Any]) -> None:
        status = data.get("status", "unverified")
        if status not in STATUSES:
            raise ContentError(f"unsupported content status: {status}")
        if not str(data.get("card_id", "")).strip() or not str(data.get("title", "")).strip() or not str(data.get("summary", "")).strip():
            raise ContentError("card_id, title and summary are required")
        day = str(data.get("calendar_day", ""))
        if len(day) != 5 or day[2] != "-":
            raise ContentError("calendar_day must use MM-DD")
        if not isinstance(data.get("image_url", ""), str):
            raise ContentError("image_url must be a string")
        tags = data.get("tags", [])
        if not isinstance(tags, list) or any(not isinstance(tag, str) or "." not in tag or tag.split(".", 1)[0] not in TAG_GROUPS for tag in tags):
            raise ContentError("tags must use controlled group.value codes")
        claims, sources = data.get("claims", []), data.get("provenance", [])
        if not isinstance(claims, list) or not isinstance(sources, list):
            raise ContentError("claims and provenance must be arrays")
        source_ids = {str(source.get("provenance_id", "")) for source in sources}
        for source in sources:
            source_type = source.get("source_type", "manual")
            if source_type not in SOURCE_TYPES:
                raise ContentError(f"unsupported source_type: {source_type}")
        _check_verified_sources(status, sources)
        for claim in claims:
            if not str(claim.get("claim_id", "")).strip() or not str(claim.get("text", "")).strip():
                raise ContentError("each claim needs claim_id and text")
            if not claim.get("provenance_id") and not claim.get("provenance") and status == "verified":
                raise ContentError("verified claims need provenance")
            if claim.get("provenance_id") and str(claim["provenance_id"]) not in source_ids:
                raise ContentError("claim references unknown provenance")

    def import_card(self, data: Mapping[str, Any], *, dry_run: bool = False) -> ContentCard:
        payload = dict(data)
        payload.setdefault("status", "unverified"); payload.setdefault("revision", 1); payload.setdefault("tags", []); payload.setdefault("claims", []); payload.setdefault("provenance", []); payload.setdefault("priority", 50); payload.setdefault("active", True); payload.setdefault("image_url", "")
        # Автотег статуса: слоты разделяют факты/мифы именно по тегам
        tags = payload["tags"] if isinstance(payload["tags"], list) else []
        status_tag = f"status.{payload['status']}"
        conflicts = [tag for tag in tags if isinstance(tag, str) and tag.startswith("status.") and tag != status_tag]
        if conflicts:
            raise ContentError(f"tags conflict with status: {conflicts}")
        payload["tags"] = [tag for tag in tags if tag != status_tag] + [status_tag]
        self._validate(payload)
        card_hash = _hash_card(payload)
        card = ContentCard(str(payload["card_id"]), int(payload["revision"]), str(payload["calendar_day"]), str(payload["title"]).strip(), str(payload["summary"]).strip(), str(payload["status"]), tuple(payload["tags"]), tuple(payload["claims"]), tuple(payload["provenance"]), int(payload["priority"]), bool(payload["active"]), str(payload.get("editorial_label") or LABELS[payload["status"]]), card_hash, str(payload["image_url"]).strip())
        if dry_run: return card
        now = _now(); self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute("INSERT OR REPLACE INTO content_cards(card_id,revision,calendar_day,title,summary,status,editorial_label,tags_json,priority,active,content_hash,image_url,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (card.card_id,card.revision,card.calendar_day,card.title,card.summary,card.status,card.editorial_label,json.dumps(card.tags,ensure_ascii=False),card.priority,int(card.active),card.content_hash,card.image_url,now,now))
            for ordinal, claim in enumerate(card.claims):
                self.conn.execute("INSERT OR REPLACE INTO content_claims VALUES(?,?,?,?,?,?)", (claim["claim_id"],card.card_id,card.revision,ordinal,claim["text"],claim.get("provenance_id", "")))
            for source in card.provenance:
                self.conn.execute("INSERT OR REPLACE INTO provenance_sources VALUES(?,?,?,?,?,?,?,?,?)", (source["provenance_id"],source.get("source_type","manual"),source.get("title",""),source.get("author",""),source.get("publication_year"),source.get("url",""),source.get("locator",""),source.get("quote",""),hashlib.sha256(json.dumps(source,sort_keys=True,ensure_ascii=False).encode()).hexdigest()))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK"); raise
        return card

    def _card(self, row: sqlite3.Row) -> ContentCard:
        claims = self.conn.execute("SELECT claim_id,claim_text,provenance_id FROM content_claims WHERE card_id=? AND revision=? ORDER BY ordinal", (row["card_id"],row["revision"])).fetchall()
        provenance = self.conn.execute("SELECT p.* FROM provenance_sources p JOIN content_claims c ON c.provenance_id=p.provenance_id WHERE c.card_id=? AND c.revision=?", (row["card_id"],row["revision"])).fetchall()
        return ContentCard(row["card_id"],row["revision"],row["calendar_day"],row["title"],row["summary"],row["status"],tuple(json.loads(row["tags_json"])),tuple({"claim_id": x["claim_id"], "text": x["claim_text"], "provenance_id": x["provenance_id"]} for x in claims),tuple(dict(x) for x in provenance),row["priority"],bool(row["active"]),row["editorial_label"],row["content_hash"],row["image_url"])

    def get_card(self, card_id: str, revision: int | None = None) -> ContentCard:
        if revision is None: row = self.conn.execute("SELECT * FROM content_cards WHERE card_id=? ORDER BY revision DESC LIMIT 1", (card_id,)).fetchone()
        else: row = self.conn.execute("SELECT * FROM content_cards WHERE card_id=? AND revision=?", (card_id,revision)).fetchone()
        if row is None: raise ContentError(f"content card not found: {card_id}")
        return self._card(row)

    def list_cards(self, calendar_day: str | None = None, statuses: Iterable[str] | None = None, *, any_day: bool = True) -> list[ContentCard]:
        statuses = tuple(statuses or STATUSES); args: list[Any] = list(statuses)
        query = f"SELECT * FROM content_cards WHERE active=1 AND status IN ({','.join('?' for _ in statuses)})"
        if calendar_day:
            if any_day:
                query += " AND calendar_day IN (?, ?)"; args.extend([calendar_day, ANY_DAY])
            else:
                query += " AND calendar_day=?"; args.append(calendar_day)
        return [self._card(row) for row in self.conn.execute(query + " ORDER BY priority DESC,card_id", args).fetchall()]

    def set_status(self, card_id: str, status: str) -> ContentCard:
        if status not in STATUSES: raise ContentError(f"unsupported content status: {status}")
        card = self.get_card(card_id)
        _check_verified_sources(status, card.provenance)
        new_tags = tuple(tag for tag in card.tags if not tag.startswith("status.")) + (f"status.{status}",)
        self.conn.execute("UPDATE content_cards SET status=?,editorial_label=?,tags_json=?,updated_at=? WHERE card_id=? AND revision=?", (status,LABELS[status],json.dumps(new_tags,ensure_ascii=False),_now(),card.card_id,card.revision)); return self.get_card(card.card_id,card.revision)

    def slots(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.conn.execute("SELECT * FROM slot_definitions WHERE enabled=1 ORDER BY ordinal")]

    @staticmethod
    def idempotency_key(local_date: str, slot_key: str, card: ContentCard) -> str:
        return hashlib.sha256(f"{local_date}|{slot_key}|{card.card_id}|{card.revision}|{card.content_hash}".encode()).hexdigest()

    def save_plan(self, local_date: str, planned: Iterable[Any], plan_version: int = PLAN_VERSION) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for slot in planned:
                revision = self.get_card(slot.card_id).revision if slot.card_id else None
                self.conn.execute("INSERT OR REPLACE INTO publication_plan VALUES(?,?,?,?,?,?,?,?)", (local_date,slot.slot_key,slot.card_id,revision,slot.score,slot.reason,slot.state,plan_version))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK"); raise

    def get_plan(self, local_date: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.conn.execute("SELECT * FROM publication_plan WHERE local_date=? ORDER BY slot_key", (local_date,)).fetchall()]

    def planned_card(self, local_date: str, slot_key: str) -> ContentCard | None:
        """Return the card planned for a slot, or None for empty/skipped slots."""
        row = self.conn.execute(
            "SELECT card_id, revision, state FROM publication_plan WHERE local_date=? AND slot_key=?",
            (local_date, slot_key),
        ).fetchone()
        if row is None or row["card_id"] is None or row["state"] == "skipped":
            return None
        return self.get_card(row["card_id"], row["revision"])

    def recent_published_card_ids(self, since_date: str) -> list[str]:
        """Card ids published since a date (inclusive), most recent first."""
        rows = self.conn.execute(
            "SELECT card_id, MAX(updated_at) AS last_at FROM publication_ledger "
            "WHERE state='published' AND local_date >= ? GROUP BY card_id "
            "ORDER BY last_at DESC, card_id DESC",
            (since_date,),
        ).fetchall()
        return [row["card_id"] for row in rows]

    def get_publication(self, local_date: str, slot_key: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM publication_ledger WHERE local_date=? AND slot_key=?", (local_date, slot_key)).fetchone()
        return dict(row) if row is not None else None

    get_ledger = get_publication

    def list_stale_publishing(self, older_than_seconds: int | float, *, local_date: str | None = None) -> list[dict[str, Any]]:
        if older_than_seconds < 0:
            raise ContentError("older_than_seconds must be non-negative")
        query = "SELECT * FROM publication_ledger WHERE state='publishing'"
        args: list[Any] = []
        if local_date is not None:
            query += " AND local_date=?"
            args.append(local_date)
        cutoff = time.time() - float(older_than_seconds)
        result = []
        for row in self.conn.execute(query + " ORDER BY updated_at", args):
            try:
                stale = _timestamp(row["updated_at"]) <= cutoff
            except (TypeError, ValueError):
                stale = True
            if stale:
                result.append(dict(row))
        return result

    list_stale_publications = list_stale_publishing

    def claim_slot(self, local_date: str, slot_key: str, card: ContentCard) -> bool:
        key = self.idempotency_key(local_date, slot_key, card)
        now = _now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute("SELECT state, attempts FROM publication_ledger WHERE local_date=? AND slot_key=?", (local_date, slot_key)).fetchone()
            if row and row["state"] in {"published", "unknown", "publishing"}:
                self.conn.execute("COMMIT")
                return False
            self.conn.execute("INSERT OR REPLACE INTO publication_ledger VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (key, local_date, slot_key, card.card_id, card.revision, "publishing", (row["attempts"] + 1 if row else 1), None, None, None, now, now))
            self.conn.execute("COMMIT")
            return True
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def _transition(self, local_date: str, slot_key: str, state: str, *, error: str = "", post_id: int | None = None, text: str | None = None, expected_idempotency_key: str | None = None, expected_states: tuple[str, ...] = ("publishing",)) -> bool:
        _validate_publication_state(state)
        if state == "published":
            _validate_post_id(post_id)
        elif post_id is not None:
            raise ContentError("only published records may have a VK post id")
        row = self.get_publication(local_date, slot_key)
        if row is None or row["state"] not in expected_states or (expected_idempotency_key and row["idempotency_key"] != expected_idempotency_key):
            return False
        updated = self.conn.execute("UPDATE publication_ledger SET state=?,vk_post_id=?,generated_text=?,error=?,updated_at=? WHERE local_date=? AND slot_key=? AND state=? AND idempotency_key=?", (state, post_id, text, error[:1000], _now(), local_date, slot_key, row["state"], row["idempotency_key"])).rowcount
        return updated == 1

    def mark_published(self, local_date: str, slot_key: str, post_id: int, text: str, *, expected_idempotency_key: str | None = None) -> bool:
        return self._transition(local_date, slot_key, "published", post_id=post_id, text=text, expected_idempotency_key=expected_idempotency_key)

    def mark_failed(self, local_date: str, slot_key: str, error: str, *, expected_idempotency_key: str | None = None) -> bool:
        return self._transition(local_date, slot_key, "failed", error=error, expected_idempotency_key=expected_idempotency_key)

    def mark_unknown(self, local_date: str, slot_key: str, error: str, *, expected_idempotency_key: str | None = None) -> bool:
        return self._transition(local_date, slot_key, "unknown", error=error, expected_idempotency_key=expected_idempotency_key)

    def reconcile_publication(self, local_date: str, slot_key: str, state: str, *, vk_post_id: int | None = None, error: str = "", generated_text: str | None = None, expected_idempotency_key: str | None = None) -> bool:
        if state not in {"published", "failed"}:
            raise ContentError("reconciliation state must be published or failed")
        return self._transition(local_date, slot_key, state, post_id=vk_post_id, error=error, text=generated_text, expected_idempotency_key=expected_idempotency_key, expected_states=("unknown",))

    reconcile_slot = reconcile_publication

    def mark_stale_unknown(self, older_than_seconds: int | float, *, local_date: str | None = None, error: str = "publishing attempt became stale; VK outcome requires reconciliation") -> list[dict[str, Any]]:
        changed = []
        for row in self.list_stale_publishing(older_than_seconds, local_date=local_date):
            if self.mark_unknown(row["local_date"], row["slot_key"], error, expected_idempotency_key=row["idempotency_key"]):
                current = self.get_publication(row["local_date"], row["slot_key"])
                if current:
                    changed.append(current)
        return changed

    reconcile_stale_publishing = mark_stale_unknown

    def publication_ledger(self, local_date: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM publication_ledger"
        args: list[Any] = []
        if local_date is not None:
            query += " WHERE local_date=?"
            args.append(local_date)
        return [dict(row) for row in self.conn.execute(query + " ORDER BY local_date,slot_key", args)]

    list_publications = publication_ledger
