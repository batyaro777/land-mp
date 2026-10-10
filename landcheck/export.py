"""Экспорт результата: XLSX (три листа), CSV, Markdown."""

from __future__ import annotations

import csv
import io
from datetime import datetime
from pathlib import Path

from landcheck.models import SOTKA_M2, BatchResult, CheckResult, Level

DISCLAIMER = (
    "Отчёт сформирован автоматически по открытым данным НСПД (nspd.gov.ru) и данным объявлений. "
    "Это риск-флаги и ценовой ориентир справочно: не является юридическим заключением "
    "и не является отчётом об оценке. "
    "У каждого факта указан источник и дата получения. Перед сделкой — выписка ЕГРН и проверка "
    "юристом или нотариусом."
)

COLUMNS = [
    "№",
    "Кадастровый номер",
    "Вердикт",
    "Причины (стоп / проверить)",
    "Справочно",
    "Цена, ₽",
    "Площадь в объявлении, сот.",
    "Площадь ЕГРН, сот.",
    "Цена за сотку, ₽",
    "Кадастровая стоимость, ₽",
    "Кад. стоимость за сотку, ₽",
    "Категория земель",
    "ВРИ",
    "Статус",
    "Границы в ЕГРН",
    "Форма собственности",
    "ЗОУИТ",
    "Территориальная зона",
    "Адрес",
    "Объявлений",
    "Данные НСПД от",
    "Ссылка",
    "Балл",
]


def _sotka(m2: float | None) -> float | None:
    return round(m2 / SOTKA_M2, 2) if m2 else None


def _money(v: float | None) -> float | None:
    return round(v) if v is not None else None


def _join(items: list[str]) -> str:
    return "\n".join(items)


def row_for(i: int, r: CheckResult) -> list:
    p, lst = r.parcel, r.listing
    reasons = [f"[{f.level.value}] {f.message}" for f in r.flags if f.level is not Level.OK]
    infos = [f.message for f in r.flags if f.level is Level.OK]
    return [
        i,
        lst.cad_num,
        r.verdict.value,
        _join(reasons),
        _join(infos),
        _money(lst.price_rub),
        _sotka(lst.declared_area_m2),
        _sotka(p.best_area_m2),
        _money(r.price_per_sotka),
        _money(p.cadastral_cost_rub),
        _money(p.cadastral_cost_per_sotka),
        p.category,
        p.permitted_use_all or None,
        p.status,
        ("да" if p.has_borders else "нет") if p.found else None,
        p.ownership_type,
        _join([z.describe() for z in p.zouit]) if p.zouit_checked else "не проверялось",
        _join([z.describe() for z in p.territorial_zones])
        if p.territorial_zone_checked
        else "не проверялось",
        p.address,
        1 + len(r.duplicates),
        p.fetched_at.strftime("%d.%m.%Y %H:%M") if p.found else None,
        lst.url,
        round(r.score),
    ]


def to_rows(batch: BatchResult) -> list[list]:
    return [row_for(i, r) for i, r in enumerate(batch.results, start=1)]


def to_csv(batch: BatchResult, path: str | Path | None = None) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\n")
    w.writerow(COLUMNS)
    for row in to_rows(batch):
        w.writerow(["" if v is None else str(v).replace("\n", " | ") for v in row])
    text = buf.getvalue()
    if path:
        Path(path).write_text(text, encoding="utf-8-sig")
    return text


def to_markdown(batch: BatchResult, *, limit: int | None = None) -> str:
    c = batch.counts
    lines = [
        f"# Проверка подборки — {batch.finished_at:%d.%m.%Y %H:%M} UTC",
        "",
        f"Цель: **{batch.request.purpose.label}**"
        + (
            f", бюджет до {batch.request.budget_rub:,.0f} ₽".replace(",", " ")
            if batch.request.budget_rub
            else ""
        ),
        "",
        f"Участков: {len(batch.results)} · стоп: {c['стоп']} · "
        f"проверить: {c['проверить']} · ок: {c['ок']}"
        + (
            f" · медиана цены за сотку: {batch.median_price_per_sotka:,.0f} ₽".replace(",", " ")
            if batch.median_price_per_sotka
            else ""
        ),
        "",
        "| № | Кадастровый номер | Вердикт | Причины | Цена, ₽ | Площадь ЕГРН, сот. | "
        "Цена/сот., ₽ | Категория | ВРИ |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    results = batch.results[:limit] if limit else batch.results
    for i, r in enumerate(results, start=1):
        p, lst = r.parcel, r.listing
        reasons = "<br>".join(f"{f.message}" for f in r.flags if f.level is not Level.OK) or "—"

        def fmt(v):
            return f"{v:,.0f}".replace(",", " ") if v is not None else "—"

        lines.append(
            f"| {i} | {lst.cad_num} | {r.verdict.value} | {reasons} | {fmt(lst.price_rub)} | "
            f"{_sotka(p.best_area_m2) or '—'} | {fmt(r.price_per_sotka)} | {p.category or '—'} | "
            f"{(p.permitted_use or '—')[:60]} |"
        )
    if batch.errors:
        lines += ["", "Ошибки получения данных:", *[f"- {e}" for e in batch.errors[:10]]]
    lines += ["", f"_{DISCLAIMER}_"]
    return "\n".join(lines)


def to_xlsx(batch: BatchResult, path: str | Path) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Подборка"
    ws.append(COLUMNS)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    fills = {
        Level.STOP.value: PatternFill("solid", fgColor="F4C7C3"),
        Level.CHECK.value: PatternFill("solid", fgColor="FCE8B2"),
        Level.OK.value: PatternFill("solid", fgColor="D9EAD3"),
    }
    for row in to_rows(batch):
        ws.append(row)
        verdict_cell = ws.cell(row=ws.max_row, column=3)
        verdict_cell.fill = fills[str(row[2])]
        for col in (4, 5, 13, 17, 18):
            ws.cell(row=ws.max_row, column=col).alignment = Alignment(
                wrap_text=True, vertical="top"
            )
    widths = [
        4,
        20,
        11,
        60,
        40,
        14,
        12,
        12,
        14,
        16,
        14,
        28,
        40,
        16,
        10,
        24,
        40,
        32,
        40,
        10,
        16,
        30,
        6,
    ]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = ws.dimensions

    wf = wb.create_sheet("Флаги")
    wf.append(["Кадастровый номер", "Правило", "Уровень", "Сообщение", "Источник"])
    for cell in wf[1]:
        cell.font = Font(bold=True)
    for r in batch.results:
        for f in r.flags:
            wf.append([r.listing.cad_num, f.rule_id, f.level.value, f.message, f.source])
    for col, w in zip("ABCDE", [20, 8, 11, 90, 70], strict=True):
        wf.column_dimensions[col].width = w

    wsrc = wb.create_sheet("Источники и поля")
    wsrc.append(["Кадастровый номер", "Поле", "Значение", "Источник", "Получено"])
    for cell in wsrc[1]:
        cell.font = Font(bold=True)
    for r in batch.results:
        if not r.parcel.found:
            continue
        for key, s in r.parcel.field_sources().items():
            value = s.value
            if isinstance(value, list):
                value = "; ".join(map(str, value)) or "—"
            wsrc.append(
                [r.listing.cad_num, key, value, s.source, s.fetched_at.strftime("%d.%m.%Y %H:%M")]
            )
    for col, w in zip("ABCDE", [20, 22, 60, 60, 16], strict=True):
        wsrc.column_dimensions[col].width = w

    wa = wb.create_sheet("О проверке")
    rows = [
        ("Сформировано", batch.finished_at.strftime("%d.%m.%Y %H:%M UTC")),
        ("Цель", batch.request.purpose.label),
        ("Бюджет, ₽", batch.request.budget_rub),
        ("Участков", len(batch.results)),
        ("Стоп", batch.counts["стоп"]),
        ("Проверить", batch.counts["проверить"]),
        ("Ок", batch.counts["ок"]),
        ("Медиана цены за сотку, ₽", _money(batch.median_price_per_sotka)),
        ("Источник данных", "НСПД — Национальная система пространственных данных, nspd.gov.ru"),
        ("Дисклеймер", DISCLAIMER),
    ]
    for k, v in rows:
        wa.append([k, v])
    wa.column_dimensions["A"].width = 28
    wa.column_dimensions["B"].width = 110
    for row in wa.iter_rows():
        row[1].alignment = Alignment(wrap_text=True, vertical="top")
    if batch.errors:
        wa.append([])
        wa.append(["Ошибки получения данных", ""])
        for e in batch.errors:
            wa.append(["", e])

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out


def export(batch: BatchResult, path: str | Path) -> Path:
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".xlsx":
        return to_xlsx(batch, p)
    if suffix == ".csv":
        to_csv(batch, p)
        return p
    if suffix in (".md", ".markdown"):
        p.write_text(to_markdown(batch), encoding="utf-8")
        return p
    raise ValueError(f"неизвестный формат экспорта: {suffix} (поддерживаются .xlsx, .csv, .md)")


def timestamped(prefix: str = "landcheck", ext: str = "xlsx") -> str:
    return f"{prefix}-{datetime.now():%Y%m%d-%H%M}.{ext}"
