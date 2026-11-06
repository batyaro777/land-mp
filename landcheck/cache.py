"""Кэш ответов НСПД в SQLite: ответ хранится с датой; повтор не чаще раза в N дней."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_TTL_DAYS = 7


class ParcelCache:
    def __init__(self, path: str | Path, ttl_days: float = DEFAULT_TTL_DAYS):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl = timedelta(days=ttl_days)
        self._lock = threading.Lock()  # одно соединение, пишут несколько потоков
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS parcels (
                cad_num TEXT PRIMARY KEY,
                fetched_at TEXT NOT NULL,
                provider TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS locations (
                cad_num TEXT PRIMARY KEY,
                fetched_at TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS regions (
                key TEXT PRIMARY KEY,
                fetched_at TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def get(self, cad_num: str, *, allow_stale: bool = False) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT fetched_at, payload FROM parcels WHERE cad_num = ?", (cad_num,)
            ).fetchone()
        if row is None:
            return None
        fetched_at = datetime.fromisoformat(row[0])
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=UTC)
        if not allow_stale and datetime.now(UTC) - fetched_at > self.ttl:
            return None
        payload = json.loads(row[1])
        payload["_cache_fetched_at"] = fetched_at.isoformat()
        return payload

    def put(self, cad_num: str, payload: dict[str, Any]) -> None:
        fetched_at = payload.get("fetched_at") or datetime.now(UTC).isoformat()
        provider = payload.get("provider", "unknown")
        body = json.dumps(payload, ensure_ascii=False, default=str)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO parcels (cad_num, fetched_at, provider, payload) "
                "VALUES (?, ?, ?, ?)",
                (cad_num, fetched_at, provider, body),
            )
            self._conn.commit()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) FROM parcels").fetchone()[0]
            oldest = self._conn.execute("SELECT MIN(fetched_at) FROM parcels").fetchone()[0]
        return {"total": total, "oldest": oldest, "path": str(self.path)}

    # --- окружение участка (модуль «Локация») -------------------------------------------
    def get_location(self, cad_num: str, *, allow_stale: bool = False) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT fetched_at, payload FROM locations WHERE cad_num = ?", (cad_num,)
            ).fetchone()
        if row is None:
            return None
        fetched_at = datetime.fromisoformat(row[0])
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=UTC)
        if not allow_stale and datetime.now(UTC) - fetched_at > self.ttl:
            return None
        return json.loads(row[1])

    def put_location(self, cad_num: str, raw: dict[str, Any]) -> None:
        fetched_at = raw.get("fetched_at") or datetime.now(UTC).isoformat()
        body = json.dumps(raw, ensure_ascii=False, default=str)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO locations (cad_num, fetched_at, payload) VALUES (?, ?, ?)",
                (cad_num, fetched_at, body),
            )
            self._conn.commit()

    # --- регион (модуль «Регион», ключ — локация + цель) ---------------------------------
    def get_region(self, key: str, *, allow_stale: bool = False) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT fetched_at, payload FROM regions WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        fetched_at = datetime.fromisoformat(row[0])
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=UTC)
        if not allow_stale and datetime.now(UTC) - fetched_at > self.ttl:
            return None
        return json.loads(row[1])

    def put_region(self, key: str, payload: dict[str, Any]) -> None:
        fetched_at = payload.get("fetched_at") or datetime.now(UTC).isoformat()
        body = json.dumps(payload, ensure_ascii=False, default=str)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO regions (key, fetched_at, payload) VALUES (?, ?, ?)",
                (key, fetched_at, body),
            )
            self._conn.commit()
