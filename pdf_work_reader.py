"""Read explicit renovation work quantities from a local PDF project.

This is a standalone, lightweight inspection tool. It does not price work, call a
server, or infer quantities absent from a drawing. Run with `python pdf_work_reader.py file.pdf`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium


CYRILLIC = re.compile(r"[А-Яа-яЁё]")
NUMBER = re.compile(r"(?<![\dА-Яа-яA-Za-z])\d+(?:[.,]\d+)?(?!\d)")
NOTE_VERBS = re.compile(
    r"\b(?:демонтаж|монтаж|устройство|укладка|оклейка|окраска|покраска|"
    r"установка|замена|шумоизоляция|гидроизоляция|облицовка|выравнивание|"
    r"шпаклевка|шпатлевка|грунтовка|стяжка|кладка|выполнить)\b",
    re.IGNORECASE,
)
ROW_HEADERS = ("обозначение", "наименование", "изображение", "описание", "площадь", "кол-во", "вид ")
EXPLICIT_QUANTITY = re.compile(
    r"(?:S\s*=\s*)?(\d+(?:[.,]\d+)?)\s*(м2|м²|кв\.?\s*м\.?|м\.п\.|п\.м\.|шт\.?)\s*$",
    re.IGNORECASE,
)
ANY_EXPLICIT_UNIT = re.compile(r"\d+(?:[.,]\d+)?\s*(?:м2|м²|кв\.?\s*м\.?|м\.п\.|п\.м\.|шт\.?)", re.IGNORECASE)
TABLE_AREA = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:кв\.?\s*м\.?|м[²2])", re.IGNORECASE)
TABLE_LENGTH = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:м\.?п\.?|п\.?м\.?)", re.IGNORECASE)
MAX_TABLE_OBJECTS = 2000
ROOM_LABELS = re.compile(
    r"^(?:\d+\s+)?(?:кухня|спальня|гостиная|санузел|ванная|прихожая|коридор|балкон|лоджия|"
    r"кабинет|детская|гардеробная|помещение|общая площадь|итого|площадь помещения)\b",
    re.IGNORECASE,
)
MATERIAL_TERMS = re.compile(
    r"обои|окраск|покраск|краск|керамогранит|плитк|кварцвинил|ламинат|паркет|"
    r"плинтус|натяжн|штукатурк|гипсокартон|\bгкл\b|т[её]пл\w*\s+пол|"
    r"гидроизоляц|молдинг|ниш\w*\s+(?:для|под)\s+(?:карниз|штор)",
    re.IGNORECASE,
)
NON_WORK_TERMS = re.compile(
    r"условное|обозначение|наименование помещ|площадь помещ|\bлистов\b|"
    r"\bмасштаб\b|\bадрес\b|\bдизайнер\b|\bзаказчик\b|\bрук\.студии\b",
    re.IGNORECASE,
)
SECTION_END = re.compile(
    r"^(?:экспликация|лист|листов|масштаб|стадия|должность|дизайнер|заказчик|"
    r"адрес|примечания|согласовано)\b",
    re.IGNORECASE,
)


def _schedule_heading(line: str) -> tuple[str, str] | None:
    match = re.search(r"\b(?:ведомост\w*|спецификац\w*)\b", line)
    if len(line) > 120 or match is None:
        return None
    line = line[match.start():]
    if "плинтус" in line:
        return "skirting", "м"
    if "тепл" in line and "пол" in line:
        return "heating_floor", "м²"
    if "освет" in line or "светильник" in line:
        return "lighting", "шт."
    if "электр" in line or "розет" in line:
        return "electrical", "шт."
    if "потол" in line:
        return "ceiling", "м²"
    if "стен" in line:
        return "walls", "м²"
    if re.search(r"\bпол(?:а|ов|ы)?\b|напольн", line):
        return "floor", "м²"
    return None


@dataclass(frozen=True)
class WorkQuantity:
    work: str
    material: str
    quantity: float
    unit: str
    page: int
    section: str


def _normalize_ocr_label(value: str) -> str:
    mixed = str.maketrans({"0": "О", "u": "и", "a": "а", "e": "е",
                           "o": "о", "p": "р", "c": "с", "x": "х"})
    return re.sub(
        r"[A-Za-zА-Яа-яЁё0-9]+",
        lambda match: match.group().translate(mixed)
        if len(CYRILLIC.findall(match.group())) >= 2 else match.group(),
        value,
    )


def _ocr_schedule_rows(result, page_number: int) -> list[WorkQuantity]:
    """Read measured rows only inside an OCR-detected work schedule."""
    if not result.txts or result.boxes is None or result.scores is None:
        return []
    boxes = []
    for text, polygon, score in zip(result.txts, result.boxes, result.scores):
        xs = [float(point[0]) for point in polygon]
        ys = [float(point[1]) for point in polygon]
        boxes.append((text, min(xs), max(xs), min(ys), max(ys), float(score)))
    found = []
    for heading in boxes:
        if not re.search(r"ведомост|спецификац", heading[0], re.IGNORECASE):
            continue
        near_title = [box[0] for box in boxes
                      if abs((box[3] + box[4]) / 2 - (heading[3] + heading[4]) / 2) < 22
                      and heading[1] - 20 <= box[1] <= heading[2] + 600]
        title = " ".join(near_title).casefold().replace("ё", "е")
        definition = _schedule_heading(title)
        if definition is None:
            continue
        section, default_unit = definition
        headers = [box for box in boxes
                   if heading[4] <= box[3] <= heading[4] + 130
                   and heading[1] - 70 <= box[1] <= heading[2] + 650
                   and re.search(r"м[2²]|м\.?п\.?|п\.?м\.?|шт|площадь|^s\b", box[0], re.IGNORECASE)
                   and "%" not in box[0]]
        if not headers:
            continue
        header = min(headers, key=lambda box: (box[3], box[1]))
        header_center_y = (header[3] + header[4]) / 2
        row_start_y = header_center_y + 0.4 * (header[4] - header[3])
        unit = "м²" if re.search(r"м[2²]|^s\b|площадь", header[0], re.IGNORECASE) else default_unit
        if re.search(r"м\.?п\.?|п\.?м\.?", header[0], re.IGNORECASE):
            unit = "м"
        if re.search(r"шт", header[0], re.IGNORECASE):
            unit = "шт."
        column_x = (header[1] + header[2]) / 2
        for value_box in boxes:
            raw = value_box[0].strip()
            if not re.fullmatch(r"\d+(?:[.,]\d+)?", raw):
                continue
            if not row_start_y < (value_box[3] + value_box[4]) / 2 < header[4] + 500:
                continue
            if abs((value_box[1] + value_box[2]) / 2 - column_x) > 70:
                continue
            quantity = float(raw.replace(",", "."))
            if not 0 < quantity <= 100_000 or value_box[5] < 0.8:
                continue
            label_boxes = [box for box in boxes
                           if heading[1] - 200 <= box[1]
                           and (box[3] + box[4]) / 2 > row_start_y
                           and box[2] < value_box[1] - 8
                           and min(box[4], value_box[4]) - max(box[3], value_box[3])
                               >= 0.35 * min(box[4] - box[3], value_box[4] - value_box[3])
                           and (box[5] >= 0.8 or
                                (box[5] >= 0.7 and MATERIAL_TERMS.search(_normalize_ocr_label(box[0]))))]
            label = " ".join(_normalize_ocr_label(box[0])
                             for box in sorted(label_boxes, key=lambda box: box[1]))
            label = re.sub(r"^\s*\d+\s+", "", label).strip()
            if len(CYRILLIC.findall(label)) < 3 or NON_WORK_TERMS.search(label) or ROOM_LABELS.match(label):
                continue
            work = _work_name(section, label)
            found.append(WorkQuantity(work, label, quantity, unit, page_number, section))
    return found


def extract_page_texts(path: Path) -> tuple[list[str], list[int], list[WorkQuantity]]:
    pdf = pdfium.PdfDocument(str(path))
    texts = [_repair_shifted_cyrillic(pdf[index].get_textpage().get_text_range()) for index in range(len(pdf))]
    unread = [index + 1 for index, text in enumerate(texts)
              if len(CYRILLIC.findall(text)) < 20 and (len(text.strip()) >= 30 or not text.strip())]
    ocr_works: list[WorkQuantity] = []
    if unread:
        model_dir = Path(__file__).parent / "models" / "ocr"
        required = ("ch_PP-OCRv5_det_mobile.onnx", "cyrillic_PP-OCRv5_rec_mobile.onnx")
        if not all((model_dir / name).is_file() for name in required):
            raise RuntimeError("Не найдены локальные OCR-модели в models/ocr")
        try:
            from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR
        except ImportError as error:
            raise RuntimeError("Не установлен rapidocr в окружении PDF-ридера") from error
        ocr = RapidOCR(params={
            "Global.model_root_dir": str(model_dir),
            "Global.use_cls": False,
            "Global.log_level": "error",
            "EngineConfig.onnxruntime.intra_op_num_threads": 2,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            "Det.ocr_version": OCRVersion.PPOCRV5,
            "Det.model_type": ModelType.MOBILE,
            "Det.lang_type": "multi",
            "Rec.ocr_version": OCRVersion.PPOCRV5,
            "Rec.model_type": ModelType.MOBILE,
            "Rec.lang_type": LangRec.CYRILLIC,
        })
        still_unread = []
        for page_number in unread:
            page = pdf[page_number - 1]
            scale = min(2.0, 2200 / max(page.get_size()))
            image = page.render(scale=scale).to_pil().convert("RGB")
            recognized = ocr(image)
            text = "\n".join(recognized.txts or [])
            if len(CYRILLIC.findall(text)) < 10:
                still_unread.append(page_number)
            else:
                ocr_works.extend(_ocr_schedule_rows(recognized, page_number))
                # OCR text from a drawing is spatially interleaved with notes;
                # parse only its measured schedule rows above.
                texts[page_number - 1] = ""
        unread = still_unread
    return texts, unread, ocr_works


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


def parse_works(page_texts: list[str]) -> list[WorkQuantity]:
    quantities: dict[tuple[str, str, float, str], WorkQuantity] = {}
    for page_number, text in enumerate(page_texts, start=1):
        section: str | None = None
        unit = "м²"
        has_allowance = False
        pending = ""
        in_table = False
        for raw in text.splitlines():
            line = " ".join(raw.split())
            if not line:
                continue
            lower = line.casefold().replace("ё", "е")
            heading = _schedule_heading(lower)
            if heading:
                section, unit = heading
                has_allowance = False
                pending = ""
                in_table = False
                continue
            if any(stop in lower for stop in (
                "экспликация помещений", "стадия лист", "примечания:",
                "состыковать швы", "спецификация ", "схема ",
            )):
                section = None
                pending = ""
                in_table = False
            if section is not None:
                if SECTION_END.match(lower):
                    section, pending, in_table = None, "", False
                    continue
                if any(lower.startswith(header) for header in ROW_HEADERS):
                    in_table = True
                    has_allowance = bool(re.search(r"\+\s*\d+(?:[.,]\d+)?\s*%", lower))
                    if "кол-во" in lower:
                        unit = "шт."
                    elif "длина" in lower:
                        unit = "м"
                    elif "площадь" in lower or "(м2)" in lower:
                        unit = "м²"
                    continue
                if not in_table:
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
    return list(quantities.values())


def _section_for_label(material: str, page_text: str) -> str:
    label = material.casefold().replace("ё", "е")
    context = page_text.casefold().replace("ё", "е")
    if "потолочный плинтус" in label or re.match(r"^карниз\b", label):
        return "ceiling_trim"
    if "плинтус" in label:
        return "skirting"
    if "теплый пол" in label:
        return "heating_floor"
    if any(word in label for word in ("розетка", "выключатель", "электровывод", "терморегулятор", "домофон")):
        return "electrical"
    if any(word in label for word in ("светильник", "люстра", "бра настенн")):
        return "lighting"
    if any(word in label for word in ("натяжной потолок", "потолок гкл", "окраска потолка")):
        return "ceiling"
    if any(word in label for word in ("кварцвинил", "ламинат", "паркет")):
        return "floor"
    if any(word in label for word in ("обои", "окраска стен", "покраска стен", "фартук", "панели", "зеркало", "молдинг")):
        return "walls"
    if "окраска" in label or "покраска" in label:
        if "потол" in context and "стен" not in context:
            return "ceiling"
        if "стен" in context:
            return "walls"
    if any(word in label for word in ("керамогранит", "плитка")):
        if any(word in context for word in ("развертки стен", "развёртки стен", "отделка стен")):
            return "walls"
        if any(word in context for word in ("напольных покрытий", "отделка полов", "план полов")):
            return "floor"
    titles = " ".join(re.findall(r"план [^\r\n]{0,60}", context))
    if re.search(r"план (?:чистовой )?отделки стен|план стен", titles):
        return "walls"
    if re.search(r"план (?:чистовой )?отделки потол|план потолков", titles):
        return "ceiling"
    if re.search(r"план напольных покрытий|план полов|план отделки пола", titles):
        return "floor"
    if NOTE_VERBS.search(label):
        return "other_work"
    return "unclassified"


def parse_drawing_legends(path: Path, page_texts: list[str], scheduled: list[WorkQuantity]) -> list[WorkQuantity]:
    """Read explicit label + quantity pairs without requiring a named plan or legend."""
    scheduled_pages = {item.page for item in scheduled}
    candidates = [
        index for index, text in enumerate(page_texts)
        if index + 1 not in scheduled_pages and ANY_EXPLICIT_UNIT.search(text)
    ]
    if not candidates:
        return []
    import pdfplumber

    found: dict[tuple[int, str, float, str], WorkQuantity] = {}
    with pdfplumber.open(path) as document:
        for index in candidates:
            text = document.pages[index].extract_text() or page_texts[index]
            text = re.sub(r"\(cid:(\d+)\)", lambda match: chr(int(match.group(1))), text)
            text = _repair_shifted_cyrillic(text)
            lines = text.splitlines()
            for row_index, line in enumerate(lines):
                match = EXPLICIT_QUANTITY.search(line.strip())
                if not match:
                    continue
                material = line[:match.start()].strip(" ,;:-")
                material = re.sub(r"^(?:примечание\s*:\s*)", "", material, flags=re.IGNORECASE)
                if ROOM_LABELS.match(material) and "," in material:
                    after_room = material.split(",", 1)[1].strip()
                    if MATERIAL_TERMS.search(after_room) or "карниз" in after_room.casefold():
                        material = after_room
                raw_unit = match.group(2).casefold()
                if ".п." in raw_unit and re.match(r"^[А-ЯA-Z]\s*\d+\b", material):
                    for previous in reversed(lines[max(0, row_index - 3):row_index]):
                        previous = previous.strip()
                        if (re.search(r"\b(?:под|для|из|с)\s*$", previous, re.IGNORECASE)
                                and MATERIAL_TERMS.search(previous)
                                and not NUMBER.search(previous)):
                            material = previous + " " + material
                            break
                if ".п." in raw_unit:
                    if row_index + 1 < len(lines) and re.search(r"плинтус|карниз", material, re.IGNORECASE):
                        following = lines[row_index + 1].strip()
                        if re.fullmatch(r"[A-Za-z][A-Za-z0-9 .-]{2,45}", following):
                            material += " " + following
                if (len(material) < 4 or len(material) > 90 or len(CYRILLIC.findall(material)) < 3
                        or ROOM_LABELS.match(material) or material.casefold().startswith(ROW_HEADERS)):
                    continue
                quantity = float(match.group(1).replace(",", "."))
                unit = "шт." if raw_unit.startswith("шт") else "м" if ".п." in raw_unit else "м²"
                if quantity <= 0:
                    continue
                section = _section_for_label(material, text)
                item = WorkQuantity(_work_name(section, material) if section != "unclassified" else material,
                                    material, quantity, unit, index + 1, section)
                found.setdefault((index + 1, material.casefold(), quantity, unit), item)
    return list(found.values())


def parse_spatial_tables(path: Path, page_texts: list[str]) -> tuple[list[WorkQuantity], set[int]]:
    """Read named work and measured quantity from separate PDF table columns."""
    import pdfplumber

    works: list[WorkQuantity] = []
    parsed_pages: set[int] = set()
    pdfium_document = pdfium.PdfDocument(str(path))
    with pdfplumber.open(path) as document:
        for page_number, text in enumerate(page_texts, 1):
            sections = {heading[0] for line in text.splitlines()
                        if (heading := _schedule_heading(line.casefold().replace("ё", "е"))) is not None}
            if len(sections) != 1:
                continue
            section = next(iter(sections))
            if section in ("lighting", "electrical"):
                continue
            # Dense vector drawings make pdfplumber's full-page table search very slow.
            if len(list(pdfium_document[page_number - 1].get_objects())) > MAX_TABLE_OBJECTS:
                continue
            for table in document.pages[page_number - 1].find_tables():
                rows = table.extract()
                header_index = next((index for index, row in enumerate(rows)
                                     if any("наименование" in (cell or "").casefold() for cell in row)
                                     and any(re.search(r"количеств|кол[-\s.]?во|площадь|длина|объ[её]м", (cell or "").casefold())
                                             for cell in row)), None)
                if header_index is None:
                    continue
                headers = [(cell or "").casefold() for cell in rows[header_index]]
                name_col = next(index for index, value in enumerate(headers) if "наименование" in value)
                amount_col = next(index for index, value in enumerate(headers)
                                  if re.search(r"количеств|кол[-\s.]?во|площадь|длина|объ[её]м", value))
                for row in rows[header_index + 1:]:
                    material = " ".join((row[name_col] or "").split())
                    amount = row[amount_col] or ""
                    candidates = [(match.start(), float(match.group(1).replace(",", ".")), unit)
                                  for pattern, unit in ((TABLE_AREA, "м²"), (TABLE_LENGTH, "м"))
                                  for match in pattern.finditer(amount)]
                    if not candidates and re.fullmatch(r"\s*\d+(?:[.,]\d+)?\s*", amount):
                        unit = "м²" if "площадь" in headers[amount_col] else "м" if "длина" in headers[amount_col] else None
                        if unit:
                            candidates.append((0, float(amount.replace(",", ".")), unit))
                    if (not material or not candidates or len(CYRILLIC.findall(material)) < 3
                            or ROOM_LABELS.match(material) or NON_WORK_TERMS.search(material)):
                        continue
                    _, quantity, unit = max(candidates)
                    if not 0 < quantity <= 100_000:
                        continue
                    item_section = ("ceiling_trim" if "потолочный плинтус" in material.casefold()
                                    or re.match(r"^карниз\b", material.casefold())
                                    else "skirting" if "плинтус" in material.casefold() else section)
                    if item_section in ("skirting", "ceiling_trim") and row[-1] and "покраск" in row[-1].casefold():
                        material += " под покраску"
                    works.append(WorkQuantity(_work_name(item_section, material), material, quantity, unit,
                                              page_number, item_section))
                    parsed_pages.add(page_number)
    pdfium_document.close()
    return works, parsed_pages


def _parse_schedule_row(line: str, pending: str, allowance: bool) -> tuple[str, float] | None:
    numbers = list(NUMBER.finditer(line))
    if not numbers:
        return None
    selected = numbers[-2] if allowance and len(numbers) > 1 else numbers[-1]
    quantity = float(selected.group().replace(",", "."))
    if re.match(r"\s*(?:мм|см)\b", line[selected.end():], re.IGNORECASE):
        return None
    # Quantities for finishes are plausible areas/lengths; a bare material type number is not.
    if quantity <= 0 or quantity > 100_000:
        return None
    material = (pending + " " + line[:selected.start()]).strip(" ,;-+")
    material = re.sub(r"\s+", " ", material)
    if (len(material) < 3 or len(CYRILLIC.findall(material)) < 3
            or material.casefold().startswith(ROW_HEADERS)
            or "\\" in material or "состыковать" in material.casefold()
            or NON_WORK_TERMS.search(material) or ROOM_LABELS.match(material)
            ):
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
        if "покраск" in name or "окраск" in name or "краска" in name:
            return "Покраска стен"
        if "фреск" in name:
            return "Отделка стен фреской"
        return "Отделка стен: " + material
    if section == "floor":
        if "керамогранит" in name or "плитк" in name:
            return "Облицовка пола керамогранитом/плиткой"
        if "кварцвинил" in name or "ламинат" in name:
            return "Укладка напольного покрытия"
        if "инженерная доска" in name:
            return "Укладка инженерной доски"
        return "Отделка пола: " + material
    if section == "ceiling":
        if "натяжн" in name:
            return "Монтаж натяжного потолка"
        return "Отделка потолка: " + material
    if section == "skirting":
        work = "Монтаж напольного плинтуса под покраску" if "покраск" in name else "Монтаж напольного плинтуса"
        if "негибкий" in name:
            return work + " (негибкий)"
        if "гибкий" in name:
            return work + " (гибкий)"
        return work
    if section == "ceiling_trim":
        if "плинтус" in name:
            return "Монтаж потолочного плинтуса под покраску" if "покраск" in name else "Монтаж потолочного плинтуса"
        return "Монтаж карниза под покраску" if "покраск" in name else "Монтаж карниза"
    if section == "heating_floor":
        if "терморегулятор" in name:
            return "Установка терморегуляторов"
        return "Устройство тёплого пола: " + material
    if section == "lighting":
        return "Монтаж освещения: " + material
    if section == "electrical":
        return "Электроточки: " + material
    return material


def collect_works(path: Path, pages: list[str], ocr_works: list[WorkQuantity]) -> list[dict]:
    table_works, _ = parse_spatial_tables(path, pages)
    quantities = parse_works(pages)
    quantities.extend(table_works)
    schedule_items = set(quantities)
    quantities.extend(parse_drawing_legends(path, pages, quantities))
    schedule_items.update(ocr_works)
    quantities.extend(ocr_works)
    works = []
    seen = set()
    seen_products: dict[tuple, set[str]] = {}
    for item in quantities:
        if (item.section in ("lighting", "electrical", "unclassified")
                or item.unit == "шт."
                or (item not in schedule_items
                    and item.section not in ("walls", "floor", "ceiling", "skirting", "ceiling_trim", "heating_floor", "other_work"))
                or NON_WORK_TERMS.search(item.material)
                or ROOM_LABELS.match(item.material)):
            continue
        key = (item.work, item.material.casefold(), item.quantity, item.unit)
        models = re.findall(r"\b[A-Za-z]{1,}[A-Za-z0-9-]*\d[A-Za-z0-9-]*\b", item.material.upper())
        product_key = (item.section, item.quantity, item.unit, models[-1]) if models else None
        page_text = pages[item.page - 1].casefold()
        source_kind = "plan" if "план " in page_text else "schedule" if "ведомост" in page_text else "other"
        opposite_kind = "schedule" if source_kind == "plan" else "plan" if source_kind == "schedule" else None
        if key in seen or (product_key is not None and opposite_kind in seen_products.get(product_key, ())):
            continue
        works.append({"work": item.work, "quantity": item.quantity, "unit": item.unit})
        seen.add(key)
        if product_key is not None:
            seen_products.setdefault(product_key, set()).add(source_kind)
    return works


def main() -> int:
    parser = argparse.ArgumentParser(description="Извлечь работы и объёмы из дизайн-проекта PDF")
    parser.add_argument("pdf", type=Path, help="Путь к PDF-файлу")
    parser.add_argument("--output", type=Path, help="Сохранить JSON")
    args = parser.parse_args()
    if not args.pdf.is_file():
        parser.error("PDF не найден")
    try:
        pages, unread, ocr_works = extract_page_texts(args.pdf)
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 2
    if unread:
        print(
            "Страницы без распознанных надписей пропущены: "
            + ", ".join(map(str, unread))
            + ". Результат по остальным страницам может быть неполным.",
            file=sys.stderr,
        )
    works = collect_works(args.pdf, pages, ocr_works)
    report = json.dumps({"works": works}, ensure_ascii=False, indent=2)
    print(report)
    if args.output:
        args.output.write_text(report + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
