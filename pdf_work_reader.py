"""Read explicit renovation work quantities from a local PDF project.

This is a standalone, lightweight inspection tool. It does not price work, call a
server, or infer quantities absent from a drawing. Run with `python pdf_work_reader.py file.pdf`.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium


CYRILLIC = re.compile(r"[А-Яа-яЁё]")
NUMBER = re.compile(r"(?<![\dА-Яа-яA-Za-z])\d+(?:[.,]\d+)?(?!\d)")
SCHEDULES = (
    ("ведомость отделки стен", "walls", "м²"),
    ("ведомость отделки полов", "floor", "м²"),
    ("ведомость материалов потолков", "ceiling", "м²"),
    ("ведомость плинтусов", "skirting", "м"),
    ("спецификация теплых полов", "heating_floor", "м²"),
    ("спецификация осветительных приборов", "lighting", "шт."),
    ("спецификация электроточек", "electrical", "шт."),
)
NOTE_VERBS = re.compile(
    r"\b(?:демонтаж|монтаж|устройство|укладка|оклейка|окраска|покраска|"
    r"установка|замена|шумоизоляция|гидроизоляция|выполнить)\b",
    re.IGNORECASE,
)
ROW_HEADERS = ("обозначение", "наименование", "изображение", "описание", "площадь", "кол-во", "вид ")
EXPLICIT_QUANTITY = re.compile(r"(?:S\s*=\s*)?(\d+(?:[.,]\d+)?)\s*(м2|м²|м\.п\.|п\.м\.|шт)\s*$", re.IGNORECASE)
FINISH_LABELS = (
    "керамогранит", "керамическая плитка", "плитка", "кварцвинил", "ламинат",
    "паркет", "плинтус", "обои", "окраска стен", "техническая окраска",
    "натяжной потолок", "покраска потолка", "окраска потолка", "фартук",
    "декоративные панели", "зеркало", "молдинг",
)


@dataclass(frozen=True)
class WorkQuantity:
    work: str
    material: str
    quantity: float
    unit: str
    page: int
    section: str


@dataclass(frozen=True)
class WorkWithoutQuantity:
    description: str
    page: int


def extract_page_texts(path: Path) -> tuple[list[str], list[int]]:
    pdf = pdfium.PdfDocument(str(path))
    texts = [_repair_shifted_cyrillic(pdf[index].get_textpage().get_text_range()) for index in range(len(pdf))]
    unread = [index + 1 for index, text in enumerate(texts)
              if len(CYRILLIC.findall(text)) < 20 and (len(text.strip()) >= 30 or not text.strip())]
    return texts, unread


def _repair_shifted_cyrillic(text: str) -> str:
    """Recover Cyrillic from PDFs whose embedded font maps every letter -581 codepoints."""
    if len(CYRILLIC.findall(text)) >= 20:
        return text
    shifted = sum(1 for char in text if ord(char) < 1000 and CYRILLIC.fullmatch(chr(ord(char) + 581)))
    if shifted < 20:
        return text
    return "".join(
        chr(ord(char) + 581) if ord(char) < 1000 and CYRILLIC.fullmatch(chr(ord(char) + 581))
        else " " if char == "\x01" else char
        for char in text
    )


def parse_works(page_texts: list[str]) -> tuple[list[WorkQuantity], list[WorkWithoutQuantity]]:
    quantities: dict[tuple[str, str, float, str], WorkQuantity] = {}
    notes: dict[str, WorkWithoutQuantity] = {}
    for page_number, text in enumerate(page_texts, start=1):
        section: str | None = None
        unit = "м²"
        has_allowance = False
        pending = ""
        for raw in text.splitlines():
            line = " ".join(raw.split())
            if not line:
                continue
            lower = line.casefold().replace("ё", "е")
            heading = next((entry for entry in SCHEDULES if entry[0] in lower), None)
            if heading:
                section, unit = heading[1], heading[2]
                has_allowance = False
                pending = ""
                continue
            if any(stop in lower for stop in (
                "экспликация помещений", "стадия лист", "примечания:",
                "состыковать швы", "спецификация ", "схема ",
            )) and heading is None:
                section = None
                pending = ""
            if section is not None:
                if any(lower.startswith(header) for header in ROW_HEADERS):
                    has_allowance = "+15%" in lower
                    if "кол-во" in lower:
                        unit = "шт."
                    elif "длина" in lower:
                        unit = "м"
                    elif "площадь" in lower or "(м2)" in lower:
                        unit = "м²"
                    continue
                parsed = _parse_schedule_row(line, pending, has_allowance)
                if parsed:
                    material, quantity = parsed
                    work = _work_name(section, material)
                    item = WorkQuantity(work, material, quantity, unit, page_number, section)
                    key_section = "equipment" if "терморегулятор" in material.casefold() else section
                    quantities.setdefault((key_section, material.casefold(), quantity, unit), item)
                    pending = ""
                elif not NUMBER.search(line) and len(line) < 80:
                    pending = (pending + " " + line).strip()
                else:
                    pending = ""
            if NOTE_VERBS.search(line) and not lower.startswith(("план ", "-", "ведомость ")):
                phrase = NOTE_VERBS.search(line)
                assert phrase is not None
                description = re.split(r"[.;]", line[phrase.start():], maxsplit=1)[0].strip()
                if (10 <= len(description) <= 110
                        and not NUMBER.search(description)
                        and not any(skip in description.casefold() for skip in (
                            "по месту", "согласно", "не предусмотрен", "перезамер", "уточнить",
                            "по тех", "перенос", "при возможности",
                        ))
                        and not description.casefold().endswith(("ого", "ой", "ых", "ую"))
                        and re.search(
                            r"\b(?:потол\w*|пол(?:а|ов|ы|у|ом)?|стен\w*|двер\w*|"
                            r"радиатор\w*|вентиляц\w*|розет\w*|плинтус\w*|проем\w*|"
                            r"сантех\w*|освещ\w*|смесител\w*|гидроизоляц\w*|"
                            r"шумоизоляц\w*)\b",
                            description.casefold(),
                        )):
                    key = " ".join(description.casefold().replace("ё", "е").split()[:2])
                    notes.setdefault(key, WorkWithoutQuantity(description, page_number))
    return list(quantities.values()), list(notes.values())


def parse_drawing_legends(path: Path, page_texts: list[str]) -> list[WorkQuantity]:
    """Read finish quantities printed beside drawing legends, not dimension marks."""
    candidates = [
        index for index, text in enumerate(page_texts)
        if ("м2" in text.casefold() or "м²" in text.casefold() or "м.п." in text.casefold())
        and any(label in text.casefold() for label in FINISH_LABELS)
        and ("план напольных покрытий" in text.casefold()
             or "развертки стен" in text.casefold()
             or "развёртки стен" in text.casefold())
    ]
    if not candidates:
        return []
    import pdfplumber

    found: dict[tuple[int, str, float, str], WorkQuantity] = {}
    with pdfplumber.open(path) as document:
        for index in candidates:
            text = document.pages[index].extract_text() or ""
            text = re.sub(r"\(cid:(\d+)\)", lambda match: chr(int(match.group(1))), text)
            text = _repair_shifted_cyrillic(text)
            lower = text.casefold()
            section = "floor" if "план напольных покрытий" in lower else "walls"
            for line in text.splitlines():
                match = EXPLICIT_QUANTITY.search(line.strip())
                if not match:
                    continue
                material = line[:match.start()].strip(" ,;:-")
                material = re.sub(r"^(?:примечание\s*:\s*)", "", material, flags=re.IGNORECASE)
                if not any(material.casefold().startswith(label) for label in FINISH_LABELS):
                    continue
                quantity = float(match.group(1).replace(",", "."))
                raw_unit = match.group(2).casefold()
                unit = "шт." if raw_unit == "шт" else "м" if ".п." in raw_unit else "м²"
                if quantity <= 0:
                    continue
                item = WorkQuantity(_work_name("skirting" if "плинтус" in material.casefold() else section, material),
                                    material, quantity, unit, index + 1, section)
                found.setdefault((index + 1, material.casefold(), quantity, unit), item)
    return list(found.values())


def parse_plan_areas(page_texts: list[str]) -> list[WorkQuantity]:
    """Read area tags only when the plan explicitly ties them to one work type."""
    items = []
    for index, text in enumerate(page_texts):
        lower = text.casefold()
        if "план теплых полов" not in lower and "план тёплых полов" not in lower:
            continue
        areas = [float(value.replace(",", ".")) for value in re.findall(
            r"\bS\s*=\s*(\d+(?:[.,]\d+)?)\s*м[2²]", text, re.IGNORECASE)]
        if areas and ("теплый пол" in lower or "тёплый пол" in lower):
            items.append(WorkQuantity("Устройство тёплого пола", "Площади зон тёплого пола",
                                      round(sum(areas), 2), "м²", index + 1, "heating_floor"))
    return items


def parse_unmeasured_ceiling(page_texts: list[str], quantities: list[WorkQuantity]) -> list[WorkWithoutQuantity]:
    if any(item.section == "ceiling" for item in quantities):
        return []
    labels = ("Штукатурка и окраска существующего потолка", "Потолок ГКЛ", "Натяжной потолок")
    for index, text in enumerate(page_texts):
        if "план потолков" in text.casefold():
            found = [WorkWithoutQuantity(label, index + 1) for label in labels if label.casefold() in text.casefold()]
            if found:
                return found
    return []


def _parse_schedule_row(line: str, pending: str, allowance: bool) -> tuple[str, float] | None:
    numbers = list(NUMBER.finditer(line))
    if not numbers:
        return None
    selected = numbers[-2] if allowance and len(numbers) > 1 else numbers[-1]
    quantity = float(selected.group().replace(",", "."))
    # Quantities for finishes are plausible areas/lengths; a bare material type number is not.
    if quantity <= 0 or quantity > 100_000:
        return None
    material = (pending + " " + line[:selected.start()]).strip(" ,;-+")
    material = re.sub(r"\s+", " ", material)
    if (len(material) < 3 or len(CYRILLIC.findall(material)) < 3
            or material.casefold().startswith(ROW_HEADERS)
            or "\\" in material or "состыковать" in material.casefold()):
        return None
    return material, quantity


def _work_name(section: str, material: str) -> str:
    name = material.casefold().replace("ё", "е")
    if section == "walls":
        if "фартук" in name:
            return "Отделка кухонного фартука"
        if "декоративные панели" in name:
            return "Монтаж декоративных панелей"
        if "зеркало" in name:
            return "Установка зеркала"
        if "молдинг" in name:
            return "Монтаж молдингов"
        if "керамогранит" in name or "плитк" in name:
            return "Облицовка стен керамогранитом/плиткой"
        if "обои" in name:
            return "Оклейка стен обоями"
        if "покраск" in name or "окраск" in name:
            return "Покраска стен"
        return "Отделка стен: " + material
    if section == "floor":
        if "керамогранит" in name or "плитк" in name:
            return "Облицовка пола керамогранитом/плиткой"
        if "кварцвинил" in name or "ламинат" in name:
            return "Укладка напольного покрытия"
        return "Отделка пола: " + material
    if section == "ceiling":
        if "натяжн" in name:
            return "Монтаж натяжного потолка"
        return "Отделка потолка: " + material
    if section == "skirting":
        return "Монтаж плинтуса: " + material
    if section == "heating_floor":
        if "терморегулятор" in name:
            return "Установка терморегуляторов"
        return "Устройство тёплого пола: " + material
    if section == "lighting":
        return "Монтаж освещения: " + material
    if section == "electrical":
        return "Электроточки: " + material
    return material


def render_report(path: Path, quantities: list[WorkQuantity], notes: list[WorkWithoutQuantity], unread: list[int], has_allowance: bool = False) -> str:
    measured = [item for item in quantities if item.section not in ("lighting", "electrical") and "терморегулятор" not in item.material.casefold()]
    components = [item for item in quantities if item not in measured]
    lines = [f"Проект: {path.name}", "", "Работы и объёмы, прямо указанные в проекте:"]
    if measured:
        for item in measured:
            amount = f"{item.quantity:g}".replace(".", ",")
            lines.append(f"• {item.work} — {amount} {item.unit} (лист PDF {item.page}; в проекте: {item.material})")
    else:
        lines.append("• Подтверждённые объёмы не найдены.")
    if components:
        lines.extend(["", "Количество изделий и электроточек по спецификациям (не объём монтажа):"])
        for item in components:
            amount = f"{item.quantity:g}".replace(".", ",")
            lines.append(f"• {item.material} — {amount} {item.unit} (лист PDF {item.page})")
    lines.extend(["", "Упомянутые работы без указанного объёма:"])
    quantity_words = [set(re.findall(r"[а-яё]{5,}", (item.work + " " + item.material).casefold())) for item in quantities]
    unmeasured = [
        item for item in notes
        if not any(len(set(re.findall(r"[а-яё]{5,}", item.description.casefold())) & words) >= 2
                   for words in quantity_words)
    ]
    if unmeasured:
        lines.extend(f"• {item.description} (лист PDF {item.page})" for item in unmeasured)
    else:
        lines.append("• Нет.")
    if unread:
        lines.extend(["", "Не удалось прочитать страницы: " + ", ".join(map(str, unread)) + "."])
    if has_allowance:
        lines.extend(["", "Знак +15% в ведомости означает запас материала; здесь указан исходный объём без запаса."])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Извлечь работы и объёмы из дизайн-проекта PDF")
    parser.add_argument("pdf", type=Path, help="Путь к PDF-файлу")
    parser.add_argument("--output", type=Path, help="Сохранить читаемый отчёт в TXT")
    args = parser.parse_args()
    if not args.pdf.is_file():
        parser.error("PDF не найден")
    pages, unread = extract_page_texts(args.pdf)
    quantities, notes = parse_works(pages)
    quantities.extend(parse_drawing_legends(args.pdf, pages))
    quantities.extend(parse_plan_areas(pages))
    notes.extend(parse_unmeasured_ceiling(pages, quantities))
    report = render_report(args.pdf, quantities, notes, unread, any("+15%" in page for page in pages))
    print(report)
    if args.output:
        args.output.write_text(report + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
