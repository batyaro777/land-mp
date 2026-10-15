"""CLI: landcheck check | fetch | monitor | rules | cache-stats."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from landcheck import __version__
from landcheck.cache import DEFAULT_TTL_DAYS, ParcelCache
from landcheck.export import export, timestamped, to_markdown
from landcheck.inputs import load_listings
from landcheck.intent import parse_request
from landcheck.models import SOTKA_M2, Level, Purpose, Request
from landcheck.pipeline import check_batch
from landcheck.providers.fixtures import FixtureProvider

app = typer.Typer(
    help="Проверка подборки земельных участков по данным НСПД: "
    "риск-флаги с объяснением и источником.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
# Кэш и TTL — те же переменные окружения, что у кабинета (`landcheck web`), чтобы прогон
# через CLI в контейнере попадал в кэш кабинета (урок 15.10: иначе кэшей два).
DEFAULT_CACHE = Path(
    os.environ.get("LANDCHECK_CACHE") or Path.home() / ".landcheck" / "cache.sqlite"
)
_DEFAULT_TTL = float(os.environ.get("LANDCHECK_TTL_DAYS") or DEFAULT_TTL_DAYS)
DEFAULT_REFERENCE = Path("data/reference_cns.txt")
DEFAULT_SNAPSHOT = Path("data/nspd_snapshot.json")


def _version(value: bool):
    if value:
        console.print(f"landcheck {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool, typer.Option("--version", "-v", callback=_version, is_eager=True, help="Версия")
    ] = False,
):
    pass


def _provider(fixtures: Path | None, *, with_zones: bool, proxy: str | None, timeout: int):
    if fixtures is not None:
        return FixtureProvider(fixtures)
    from landcheck.providers.nspd import LiveNspdProvider

    return LiveNspdProvider(with_zones=with_zones, proxy=proxy, timeout=timeout)


def _print_summary(batch, limit: int) -> None:
    c = batch.counts
    console.print(
        f"\n[bold]Участков:[/] {len(batch.results)}  "
        f"[red]стоп {c['стоп']}[/]  [yellow]проверить {c['проверить']}[/]  [green]ок {c['ок']}[/]"
        + (
            f"  медиана {batch.median_price_per_sotka:,.0f} ₽/сот.".replace(",", " ")
            if batch.median_price_per_sotka
            else ""
        )
        + f"  за {(batch.finished_at - batch.started_at).total_seconds():.0f} с"
    )
    table = Table(show_lines=False, header_style="bold")
    for col in (
        "№",
        "Кадастровый номер",
        "Вердикт",
        "Причины",
        "Цена, ₽",
        "ЕГРН, сот.",
        "Категория",
    ):
        table.add_column(col, overflow="fold")
    colors = {Level.STOP: "red", Level.CHECK: "yellow", Level.OK: "green"}
    for i, r in enumerate(batch.results[:limit], start=1):
        reasons = "; ".join(f.message for f in r.flags if f.level is not Level.OK) or "—"
        price = f"{r.listing.price_rub:,.0f}".replace(",", " ") if r.listing.price_rub else "—"
        area = f"{r.parcel.best_area_m2 / SOTKA_M2:.1f}" if r.parcel.best_area_m2 else "—"
        table.add_row(
            str(i),
            r.listing.cad_num,
            f"[{colors[r.verdict]}]{r.verdict.value}[/]",
            reasons[:160],
            price,
            area,
            (r.parcel.category or "—")[:30],
        )
    console.print(table)
    if len(batch.results) > limit:
        console.print(f"… ещё {len(batch.results) - limit} строк в файле экспорта")
    if batch.errors:
        console.print(f"[yellow]Ошибки получения данных ({len(batch.errors)}):[/]")
        for e in batch.errors[:5]:
            console.print(f"  • {e}")


@app.command()
def check(
    source: Annotated[str, typer.Argument(help="Файл .csv/.xlsx/.txt или строка с номерами")],
    purpose: Annotated[
        Purpose, typer.Option("--purpose", "-p", help="Цель покупателя")
    ] = Purpose.ANY,
    query: Annotated[
        str | None, typer.Option("--query", "-q", help="Запрос текстом: «ИЖС до 3 млн, 8–12 соток»")
    ] = None,
    budget: Annotated[float | None, typer.Option(help="Бюджет, ₽")] = None,
    min_area: Annotated[float | None, typer.Option(help="Мин. площадь, соток")] = None,
    max_area: Annotated[float | None, typer.Option(help="Макс. площадь, соток")] = None,
    area_unit: Annotated[
        str, typer.Option(help="Единица площади во входе без подписи: sotka | m2")
    ] = "sotka",
    out: Annotated[Path | None, typer.Option("--out", "-o", help="Файл .xlsx / .csv / .md")] = None,
    md: Annotated[bool, typer.Option("--md", help="Вывести Markdown-таблицу в stdout")] = False,
    fixtures: Annotated[
        Path | None, typer.Option(help="Папка JSON-фикстур вместо живого НСПД (офлайн)")
    ] = None,
    cache: Annotated[Path, typer.Option(help="SQLite-кэш ответов НСПД")] = DEFAULT_CACHE,
    ttl_days: Annotated[float, typer.Option(help="Срок кэша, дней")] = _DEFAULT_TTL,
    refresh: Annotated[bool, typer.Option(help="Игнорировать кэш, запросить заново")] = False,
    workers: Annotated[int, typer.Option(help="Параллельных запросов к НСПД")] = 2,
    no_zones: Annotated[bool, typer.Option(help="Не проверять ЗОУИТ и терзоны (быстрее)")] = False,
    proxy: Annotated[
        str | None, typer.Option(help="Прокси для НСПД, напр. socks5://host:port")
    ] = None,
    timeout: Annotated[int, typer.Option(help="Таймаут запроса к НСПД, с")] = 30,
    limit: Annotated[int, typer.Option(help="Строк в консольной таблице")] = 30,
):
    """Пакетная проверка подборки: таблица «стоп / проверить / ок» с причинами."""
    if area_unit not in ("sotka", "m2"):
        raise typer.BadParameter("area_unit: sotka или m2")
    listings = load_listings(source, area_unit)  # type: ignore[arg-type]
    if not listings:
        console.print("[red]Во входе не найдено ни одного кадастрового номера.[/]")
        raise typer.Exit(code=1)

    request = parse_request(query) if query else Request()
    if purpose is not Purpose.ANY:
        request.purpose = purpose
    if budget is not None:
        request.budget_rub = budget
    if min_area is not None:
        request.min_area_m2 = min_area * SOTKA_M2
    if max_area is not None:
        request.max_area_m2 = max_area * SOTKA_M2

    provider = _provider(fixtures, with_zones=not no_zones, proxy=proxy, timeout=timeout)
    pcache = ParcelCache(cache, ttl_days=ttl_days) if fixtures is None else None
    console.print(
        f"Проверяю {len(listings)} строк, цель «{request.purpose.label}», "
        f"источник: {'фикстуры ' + str(fixtures) if fixtures else 'НСПД'}"
    )

    def progress(done: int, total: int, cn: str) -> None:
        console.print(f"  [{done}/{total}] {cn}", highlight=False)

    try:
        batch = check_batch(
            listings,
            request,
            provider,
            pcache,
            refresh=refresh,
            workers=workers,
            progress=progress if len(listings) <= 400 else None,
        )
    finally:
        if pcache is not None:
            pcache.close()
        close = getattr(provider, "close", None)
        if close:
            close()

    _print_summary(batch, limit)
    if md:
        console.print()
        print(to_markdown(batch))
    target = out or Path("out") / timestamped()
    path = export(batch, target)
    console.print(f"\nСохранено: [bold]{path}[/]")


@app.command()
def fetch(
    cad_nums: Annotated[list[str], typer.Argument(help="Кадастровые номера")],
    out: Annotated[Path, typer.Option("--out", "-o", help="Папка для JSON")] = Path("fixtures"),
    no_zones: Annotated[bool, typer.Option(help="Без ЗОУИТ и терзон")] = False,
    proxy: Annotated[str | None, typer.Option(help="Прокси для НСПД")] = None,
    timeout: Annotated[int, typer.Option(help="Таймаут, с")] = 30,
):
    """Скачать сырые ответы НСПД в JSON — для фикстур, отладки и эталона."""
    from landcheck.inputs import CAD_RE, normalize_cad_num
    from landcheck.providers.nspd import LiveNspdProvider

    provider = LiveNspdProvider(with_zones=not no_zones, proxy=proxy, timeout=timeout)
    store = FixtureProvider(out)
    numbers = [normalize_cad_num(m.group(0)) for s in cad_nums for m in CAD_RE.finditer(s)]
    try:
        for cn in numbers:
            payload = provider.fetch_raw(cn)
            path = store.save(payload)
            status = "найден" if payload["found"] else "НЕ НАЙДЕН"
            errs = f" ошибки: {'; '.join(payload['errors'])}" if payload["errors"] else ""
            console.print(f"{cn}: {status} → {path}{errs}")
    finally:
        provider.close()


@app.command()
def monitor(
    reference: Annotated[Path, typer.Option(help="Файл с эталонными номерами")] = DEFAULT_REFERENCE,
    snapshot: Annotated[Path, typer.Option(help="Снимок формата НСПД")] = DEFAULT_SNAPSHOT,
    update: Annotated[bool, typer.Option(help="Пересоздать снимок по текущим ответам")] = False,
    fixtures: Annotated[Path | None, typer.Option(help="Фикстуры вместо НСПД")] = None,
    proxy: Annotated[str | None, typer.Option(help="Прокси для НСПД")] = None,
    timeout: Annotated[int, typer.Option(help="Таймаут, с")] = 30,
):
    """Ежедневный автотест: эталонные номера против снимка. Код 2 — формат НСПД изменился."""
    from landcheck.monitor import (
        build_snapshot,
        load_reference,
        load_snapshot,
        run_monitor,
        save_snapshot,
    )

    cad_nums = load_reference(reference)
    if not cad_nums:
        console.print(f"[red]В {reference} нет кадастровых номеров.[/]")
        raise typer.Exit(code=1)
    provider = _provider(fixtures, with_zones=False, proxy=proxy, timeout=timeout)
    try:
        if update:
            snap = build_snapshot(provider, cad_nums)
            path = save_snapshot(snap, snapshot)
            found = sum(1 for p in snap["parcels"].values() if p["found"])
            console.print(f"Снимок сохранён: {path} (найдено {found}/{len(cad_nums)})")
            return
        report = run_monitor(provider, cad_nums, load_snapshot(snapshot))
    finally:
        close = getattr(provider, "close", None)
        if close:
            close()
    console.print(report.summary())
    if report.drift:
        raise typer.Exit(code=2)


@app.command()
def rules():
    """Каталог риск-правил."""
    from landcheck.rules import (
        RULES,
        catalog,  # noqa: F401 — регистрирует правила
    )

    table = Table(header_style="bold")
    table.add_column("ID")
    table.add_column("Правило")
    table.add_column("Что проверяет")
    for r in RULES:
        table.add_row(r.id, r.title, r.description)
    console.print(table)


@app.command("cache-stats")
def cache_stats(cache: Annotated[Path, typer.Option(help="SQLite-кэш")] = DEFAULT_CACHE):
    """Размер и возраст кэша НСПД."""
    if not cache.exists():
        console.print(f"Кэша нет: {cache}")
        return
    c = ParcelCache(cache)
    try:
        print(json.dumps(c.stats(), ensure_ascii=False, indent=2))
    finally:
        c.close()


@app.command()
def web(
    host: Annotated[str, typer.Option(help="Адрес")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Порт")] = 8000,
    reload: Annotated[bool, typer.Option(help="Автоперезапуск при правках (разработка)")] = False,
):
    """Веб-кабинет брокера. Настройки — переменные LANDCHECK_* (см. README)."""
    import uvicorn

    uvicorn.run("landcheck.web.app:create_app", factory=True, host=host, port=port, reload=reload)


@app.command()
def invite(
    count: Annotated[int, typer.Option(help="Сколько кодов создать")] = 1,
    note: Annotated[str | None, typer.Option(help="Пометка: кому")] = None,
):
    """Создать коды приглашения для входа в кабинет."""
    from landcheck.web.config import settings
    from landcheck.web.db import create_invites, init_db, session

    init_db(settings.db_path)
    with session() as db:
        codes = create_invites(db, count, note)
    for c in codes:
        print(c)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
