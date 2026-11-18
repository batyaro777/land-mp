"""Сделки Росреестра в кабинете: загрузка квартальных файлов, хранение и цена лота по ним.

Файлы лежат в открытом каталоге Росреестра (`rr_deals.LISTING_URL`), новый квартал появляется
раз в три месяца. Фоновый поток раз в полсуток смотрит каталог и догружает недостающие из
последних `KEEP_PERIODS` кварталов. Сервер Росреестра с зарубежных адресов не отвечает,
поэтому ходим через RU-шлюз; без шлюза и в демо-режиме модуль выключен.

Строки сделок — справочные данные, которые всегда можно скачать заново, поэтому лежат в
отдельном файле рядом с базой кабинета: в её резервные копии они не попадают и не раздувают.
"""

from __future__ import annotations

import io
import logging
import sqlite3
import threading
import time
import zipfile
from collections import OrderedDict
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from landcheck import rr_deals, ruproxy
from landcheck.web.config import settings

log = logging.getLogger("landcheck.web.rr_sync")

KEEP_PERIODS = 4  # год сделок: меньше — мало сопоставимых, больше — цены устаревают
CHECK_HOURS = 12.0
REQUEST_PAUSE_S = 3.0
MAX_FILE_MB = 400
INDEX_REGIONS = 12  # сколько кадастровых округов держим в памяти разом
BATCH = 5000

_lock = threading.Lock()
_version = 0
_index: OrderedDict[str, tuple[tuple[str, int], rr_deals.DealIndex | None]] = OrderedDict()
_thread: threading.Thread | None = None
_error: str | None = None


def store_path() -> Path:
    return Path(settings.db_path).parent / "rosreestr_deals.sqlite"


def enabled() -> bool:
    return settings.rr_deals and settings.fixtures is None and ruproxy.enabled()


def version() -> int:
    """Растёт при каждой загрузке квартала: по нему устаревает расчёт списка лотов."""
    return _version


def _connect() -> sqlite3.Connection:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(
        "CREATE TABLE IF NOT EXISTS files (id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE, "
        "period TEXT NOT NULL, rows INTEGER NOT NULL, loaded_at TEXT NOT NULL)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS deals (file INTEGER NOT NULL, region TEXT NOT NULL, "
        "district TEXT NOT NULL, q INTEGER NOT NULL, grp TEXT NOT NULL, area REAL NOT NULL, "
        "price REAL NOT NULL, n INTEGER NOT NULL, period TEXT NOT NULL)"
    )
    con.execute("CREATE INDEX IF NOT EXISTS ix_deals_region ON deals(region)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_deals_file ON deals(file)")
    con.execute(
        "CREATE TABLE IF NOT EXISTS skipped (url TEXT PRIMARY KEY, rev INTEGER NOT NULL, "
        "period TEXT NOT NULL, reason TEXT NOT NULL)"
    )
    return con


# --- загрузка -------------------------------------------------------------------------------
def load_lines(period: str, url: str, lines: Iterable[str]) -> int:
    """Строки одного файла сделок → хранилище. Файл грузится целиком или никак: при ошибке
    разбора прежние строки этого файла остаются. Возвращает число сохранённых строк."""
    global _version
    insert = "INSERT INTO deals VALUES (?,?,?,?,?,?,?,?,?)"
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        now = datetime.now(UTC).isoformat(timespec="seconds")
        con.execute(
            "INSERT INTO files (url, period, rows, loaded_at) VALUES (?,?,0,?) "
            "ON CONFLICT(url) DO UPDATE SET period = excluded.period, "
            "loaded_at = excluded.loaded_at",
            (url, period, now),
        )
        file_id = con.execute("SELECT id FROM files WHERE url = ?", (url,)).fetchone()[0]
        con.execute("DELETE FROM deals WHERE file = ?", (file_id,))
        count, batch = 0, []
        for row in rr_deals.parse_rows(lines):
            if row["period"] != period:
                continue  # чужой квартал в файле — не смешиваем
            batch.append(
                (
                    file_id,
                    row["region"],
                    row["district"],
                    row["q"],
                    row["group"],
                    row["area"],
                    row["price"],
                    row["n"],
                    period,
                )
            )
            if len(batch) >= BATCH:
                con.executemany(insert, batch)
                count += len(batch)
                batch = []
        if batch:
            con.executemany(insert, batch)
            count += len(batch)
        con.execute("UPDATE files SET rows = ? WHERE id = ?", (count, file_id))
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    with _lock:
        _version += 1
        _index.clear()
    return count


def load_zip(period: str, url: str, path: Path) -> int:
    """Архив квартала с одним CSV внутри (UTF-8, разделитель «~»)."""
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"в архиве ожидался один CSV, найдено {len(names)}")
        with zf.open(names[0]) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8", newline="")
            return load_lines(period, url, text)


def _download(client: httpx.Client, url: str, dest: Path) -> int:
    size = 0
    with client.stream("GET", url) as resp:
        resp.raise_for_status()
        with dest.open("wb") as fh:
            for chunk in resp.iter_bytes(1 << 20):
                size += len(chunk)
                if size > MAX_FILE_MB * 1024 * 1024:
                    raise ValueError(f"файл больше {MAX_FILE_MB} МБ")
                fh.write(chunk)
    return size


def _skip(url: str, period: str, reason: str) -> None:
    con = _connect()
    try:
        con.execute(
            "INSERT OR REPLACE INTO skipped VALUES (?,?,?,?)",
            (url, rr_deals.PARSER_REV, period, reason),
        )
        con.commit()
    finally:
        con.close()


def loaded() -> dict[str, dict[str, Any]]:
    """Что уже загружено: период → число файлов и строк."""
    con = _connect()
    try:
        rows = con.execute(
            "SELECT period, COUNT(*), SUM(rows), MAX(loaded_at) FROM files GROUP BY period"
        ).fetchall()
    finally:
        con.close()
    return {p: {"files": f, "rows": r or 0, "loaded_at": at} for p, f, r, at in rows}


def sync(
    client: httpx.Client, *, sleep=time.sleep, keep: int = KEEP_PERIODS
) -> tuple[dict[str, int], list[str]]:
    """Один проход: каталог Росреестра → недостающие файлы последних `keep` кварталов.
    Возвращает, сколько строк загружено по каждому новому периоду, и что не удалось: файл
    другого формата или оборванная загрузка не мешают остальным кварталам. Кварталы старше
    `keep` последних убираем."""
    global _version
    resp = client.get(rr_deals.LISTING_URL)
    resp.raise_for_status()
    folders = rr_deals.listing_folders(resp.content.decode("cp1251", "replace"))[:keep]
    if not folders:
        raise ValueError("в каталоге Росреестра не нашлись папки кварталов")
    con = _connect()
    try:
        have = {u for (u,) in con.execute("SELECT url FROM files WHERE rows > 0")}
        skipped = dict(
            con.execute(
                "SELECT url, reason FROM skipped WHERE rev = ?", (rr_deals.PARSER_REV,)
            ).fetchall()
        )
    finally:
        con.close()
    done: dict[str, int] = {}
    problems: list[str] = []
    tmp = store_path().with_suffix(".part")
    for period, folder_url in folders:
        sleep(REQUEST_PAUSE_S)
        try:
            page = client.get(folder_url)
            page.raise_for_status()
        except httpx.HTTPError as exc:
            problems.append(f"{rr_deals.period_label(period)}: {type(exc).__name__}")
            continue
        files = rr_deals.listing_files(page.content.decode("cp1251", "replace"), folder_url)
        if not files:
            problems.append(f"{rr_deals.period_label(period)}: файл сделок в папке не найден")
        for file_period, url in files:
            if url in have or file_period != period:
                continue
            if url in skipped:  # формат этой версией разбора не читается — заново не качаем
                problems.append(f"{rr_deals.period_label(period)}: {skipped[url]}")
                continue
            sleep(REQUEST_PAUSE_S)
            try:
                _download(client, url, tmp)
                done[period] = done.get(period, 0) + load_zip(period, url, tmp)
            except (ValueError, zipfile.BadZipFile) as exc:
                reason = str(exc)[:120] or type(exc).__name__
                problems.append(f"{rr_deals.period_label(period)}: {reason}")
                _skip(url, period, reason)
            except (httpx.HTTPError, OSError) as exc:  # связь: попробуем в следующий проход
                problems.append(f"{rr_deals.period_label(period)}: {type(exc).__name__}")
            finally:
                tmp.unlink(missing_ok=True)
    keep_periods = [p for p, _ in folders]
    marks = ",".join("?" * len(keep_periods))
    con = _connect()
    try:
        old = con.execute(
            f"SELECT COUNT(*) FROM files WHERE period NOT IN ({marks})", keep_periods
        ).fetchone()[0]
        if old:
            con.execute(f"DELETE FROM deals WHERE period NOT IN ({marks})", keep_periods)
            con.execute(f"DELETE FROM files WHERE period NOT IN ({marks})", keep_periods)
            con.commit()
            with _lock:
                _version += 1
                _index.clear()
    finally:
        con.close()
    return done, problems


def start_loop(*, first_delay_s: float = 300.0) -> threading.Thread | None:
    """Фоновая загрузка сделок. Без RU-шлюза, в демо-режиме и при LANDCHECK_RR_DEALS=0 —
    не запускается."""
    global _thread
    if not enabled() or (_thread is not None and _thread.is_alive()):
        return None

    def loop() -> None:
        global _error
        time.sleep(first_delay_s)
        while True:
            try:
                with ruproxy.client(timeout=300) as client:
                    done, problems = sync(client)
                _error = "; ".join(problems)[:300] or None
                if done or problems:
                    log.warning("сделки Росреестра: загружено %s, не удалось %s", done, problems)
            except Exception as exc:  # noqa: BLE001 — поток не должен умереть от одной ошибки
                _error = f"{type(exc).__name__}: {str(exc)[:160]}"
                log.warning("сделки Росреестра: %s", _error)
            time.sleep(CHECK_HOURS * 3600)

    _thread = threading.Thread(target=loop, name="rr-deals", daemon=True)
    _thread.start()
    return _thread


# --- цена для лота --------------------------------------------------------------------------
def index_of(cad_region: str) -> rr_deals.DealIndex | None:
    """Сделки кадастрового округа в памяти; держим несколько последних округов."""
    key = (str(store_path()), _version)
    with _lock:
        hit = _index.get(cad_region)
        if hit and hit[0] == key:
            _index.move_to_end(cad_region)
            return hit[1]
    if not store_path().exists():
        index = None
    else:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT district, q, grp, area, price, n, period FROM deals WHERE region = ?",
                (cad_region,),
            ).fetchall()
        finally:
            con.close()
        index = rr_deals.DealIndex(rows) if rows else None
    with _lock:
        _index[cad_region] = (key, index)
        _index.move_to_end(cad_region)
        while len(_index) > INDEX_REGIONS:
            _index.popitem(last=False)
    return index


def market(lot: dict[str, Any]) -> dict[str, Any] | None:
    """Цена лота по сделкам Росреестра в его квартале или районе; None — сделок мало."""
    key = rr_deals.quarter_key(lot.get("quarter"))
    if key is None:
        return None
    return rr_deals.deals_market(lot, index_of(key[0]))


def market_fn() -> Callable[[dict[str, Any]], dict[str, Any] | None] | None:
    """Функция оценки для списка лотов; None — сделки ещё не загружены."""
    return market if store_path().exists() and loaded() else None


def status(cad_region: str) -> dict[str, Any]:
    """Строка состояния для страницы: какие кварталы загружены и сколько сделок в округе."""
    if not store_path().exists():
        return {"enabled": enabled(), "periods": [], "error": _error}
    con = _connect()
    try:
        rows = con.execute(
            "SELECT period, COUNT(*), SUM(n) FROM deals WHERE region = ? GROUP BY period",
            (cad_region,),
        ).fetchall()
    finally:
        con.close()
    have = loaded()
    periods = [
        {
            "period": p,
            "label": rr_deals.period_label(p),
            "rows": next((r for q, r, _ in rows if q == p), 0),
            "deals": next((d for q, _, d in rows if q == p), 0) or 0,
            "loaded_at": (have[p]["loaded_at"] or "")[:10],
        }
        for p in sorted(have, reverse=True)
    ]
    return {"enabled": enabled(), "periods": periods, "error": _error}
