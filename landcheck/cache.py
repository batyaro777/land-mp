"""Кэш ответов НСПД в SQLite: ответ хранится с датой; повтор не чаще раза в N дней."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_TTL_DAYS = 7


class ParcelCache:
    def __init__(self, path: str | Path, ttl_days: float = DEFAULT_TTL_DAYS):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl = timedelta(days=ttl_days)
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
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def get(self, cad_num: str, *, allow_stale: bool = False) -> dict[str, Any] | None:
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
        self._conn.execute(
            "INSERT OR REPLACE INTO parcels (cad_num, fetched_at, provider, payload) "
            "VALUES (?, ?, ?, ?)",
            (cad_num, fetched_at, provider, json.dumps(payload, ensure_ascii=False, default=str)),
        )
        self._conn.commit()

    def stats(self) -> dict[str, Any]:
        total = self._conn.execute("SELECT COUNT(*) FROM parcels").fetchone()[0]
        oldest = self._conn.execute("SELECT MIN(fetched_at) FROM parcels").fetchone()[0]
        return {"total": total, "oldest": oldest, "path": str(self.path)}
