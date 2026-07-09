"""Сборка бенчмарк-корпуса: WTQ + русская синтетика → DOCX/PPTX + golden.jsonl.

Детерминированно (seed=42). Выход:
  bench/corpus/*.docx|*.pptx — файлы-вложения для агента
  bench/golden.jsonl        — {qid, file, fmt, lang, question, answer}
"""

from __future__ import annotations

import csv
import html
import json
import random
import re
import sys
from pathlib import Path

from docx import Document as Docx
from pptx import Presentation
from pptx.util import Inches, Pt

BENCH = Path(__file__).parent
RAW = BENCH / "raw" / "WikiTableQuestions"
CORPUS = BENCH / "corpus"
CORPUS.mkdir(exist_ok=True)

random.seed(42)

MAX_ROWS, MAX_COLS = 14, 7
N_WTQ_QUESTIONS = 60
N_WTQ_PPTX = 12  # часть WTQ-таблиц уходит в PPTX вместо DOCX


def load_wtq() -> list[dict]:
    """Читает WTQ, фильтрует до exact-match-able вопросов на компактных таблицах."""
    rows = []
    with open(RAW / "data" / "pristine-unseen-tables.tsv") as f:
        reader = csv.DictReader(f, delimiter="\t")
        rows = list(reader)
    random.shuffle(rows)

    picked, used_tables = [], {}
    for r in rows:
        answer = r["targetValue"]
        if "|" in answer:  # мульти-ответы не exact-match'атся надёжно
            continue
        if len(answer) > 40 or not answer.strip():
            continue
        table_rel = r["context"]  # например csv/204-csv/590.csv
        if used_tables.get(table_rel, 0) >= 2:  # максимум 2 вопроса на таблицу
            continue
        table_path = RAW / table_rel
        if not table_path.exists():
            continue
        try:
            with open(table_path, newline="") as tf:
                cells = list(csv.reader(tf))
        except Exception:
            continue
        if not cells or len(cells) > MAX_ROWS or len(cells[0]) > MAX_COLS:
            continue
        if any(len(c) > 60 for row in cells for c in row):
            continue
        if any(len(row) != len(cells[0]) for row in cells):
            continue
        picked.append(
            {
                "question": html.unescape(r["utterance"]),
                "answer": html.unescape(answer.strip()),
                "table": [[html.unescape(c) for c in row] for row in cells],
                "table_id": table_rel,
                "lang": "en",
            }
        )
        used_tables[table_rel] = used_tables.get(table_rel, 0) + 1
        if len(picked) >= N_WTQ_QUESTIONS:
            break
    return picked


# ---------- Русская синтетика ----------

RU_TOPICS = [
    ("Бюджет отдела", ["Статья", "Q1", "Q2", "Q3"],
     [["Маркетинг", 120, 150, 90], ["Разработка", 300, 320, 340],
      ["Поддержка", 80, 85, 70], ["Инфраструктура", 200, 180, 210],
      ["Обучение", 40, 30, 55]]),
    ("Продажи по регионам", ["Регион", "Январь", "Февраль", "Март"],
     [["Москва", 500, 520, 610], ["Санкт-Петербург", 300, 280, 330],
      ["Казань", 120, 150, 140], ["Новосибирск", 90, 95, 105]]),
    ("Сотрудники проекта", ["Имя", "Роль", "Ставка", "Часы"],
     [["Иванов", "Инженер", 2500, 160], ["Петрова", "Дизайнер", 2200, 120],
      ["Сидоров", "Аналитик", 2000, 140], ["Кузнецова", "Менеджер", 2800, 160]]),
    ("Складские остатки", ["Товар", "Остаток", "Мин. запас", "Цена"],
     [["Ноутбук A1", 24, 10, 85000], ["Монитор M3", 56, 20, 32000],
      ["Клавиатура K2", 140, 50, 4500], ["Мышь X5", 210, 80, 2100],
      ["Докстанция D7", 18, 15, 12000]]),
]


def gen_russian() -> list[dict]:
    """~20 таблиц-вариаций с вопросами трёх типов: факт, максимум, сумма."""
    out = []
    for variant in range(5):
        for title, header, base_rows in RU_TOPICS:
            rows = [
                [r[0]] + [
                    v + random.randint(-9, 9) if isinstance(v, int) else v
                    for v in r[1:]
                ]
                for r in base_rows
            ]
            cells = [header] + [[str(c) for c in r] for r in rows]
            tid = f"ru-{title}-{variant}"
            col = random.randrange(1, len(header))
            row = random.randrange(len(rows))
            out.append({
                "question": f"{title}: какое значение «{header[col]}» у «{rows[row][0]}»?",
                "answer": str(rows[row][col]),
                "table": cells, "table_id": tid, "lang": "ru",
            })
            if variant < 2:  # агрегации — на части таблиц
                num_col = 1
                vals = [r[num_col] for r in rows]
                out.append({
                    "question": f"{title}: у кого максимальное значение «{header[num_col]}»? Ответь одним названием.",
                    "answer": str(rows[vals.index(max(vals))][0]),
                    "table": cells, "table_id": tid, "lang": "ru",
                })
    random.shuffle(out)
    return out[:30]


# ---------- Рендеры ----------

def render_docx(title: str, cells: list[list[str]], path: Path):
    d = Docx()
    d.add_heading(title, level=1)
    d.add_paragraph("Документ для анализа. Данные в таблице ниже.")
    t = d.add_table(rows=len(cells), cols=len(cells[0]))
    t.style = "Table Grid"
    for i, row in enumerate(cells):
        for j, val in enumerate(row):
            t.rows[i].cells[j].text = str(val)
    d.save(path)


def render_pptx(title: str, cells: list[list[str]], path: Path):
    p = Presentation()
    slide = p.slides.add_slide(p.slide_layouts[5])  # title only
    slide.shapes.title.text = title
    rows, cols = len(cells), len(cells[0])
    shape = slide.shapes.add_table(
        rows, cols, Inches(0.5), Inches(1.5), Inches(9), Inches(0.4 * rows)
    )
    for i, row in enumerate(cells):
        for j, val in enumerate(row):
            cell = shape.table.cell(i, j)
            cell.text = str(val)
            cell.text_frame.paragraphs[0].font.size = Pt(12)
    p.save(path)


def safe_name(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9а-яА-Я]+", "-", s)[:40].strip("-")


def main():
    golden = []
    items = load_wtq() + gen_russian()
    print(f"вопросов отобрано: {len(items)}")

    # один файл на таблицу; формат чередуем
    file_by_table: dict[str, tuple[str, str]] = {}
    pptx_budget = N_WTQ_PPTX + 8  # часть русских тоже в pptx
    for it in items:
        tid = it["table_id"]
        if tid not in file_by_table:
            idx = len(file_by_table)
            title = tid.split("/")[-1].replace(".csv", "") if it["lang"] == "en" else tid.split("-")[1]
            base = f"{idx:03d}-{safe_name(title)}"
            if pptx_budget > 0 and idx % 4 == 3:
                fname = base + ".pptx"
                render_pptx(str(title), it["table"], CORPUS / fname)
                pptx_budget -= 1
            else:
                fname = base + ".docx"
                render_docx(str(title), it["table"], CORPUS / fname)
            file_by_table[tid] = (fname, "pptx" if fname.endswith("pptx") else "docx")
        fname, fmt = file_by_table[tid]
        golden.append({
            "qid": f"q{len(golden):03d}", "file": fname, "fmt": fmt,
            "lang": it["lang"], "question": it["question"], "answer": it["answer"],
        })

    with open(BENCH / "golden.jsonl", "w") as f:
        for g in golden:
            f.write(json.dumps(g, ensure_ascii=False) + "\n")

    by_fmt = {}
    for g in golden:
        k = (g["fmt"], g["lang"])
        by_fmt[k] = by_fmt.get(k, 0) + 1
    print(f"файлов: {len(file_by_table)}, вопросов: {len(golden)}, срезы: {by_fmt}")


if __name__ == "__main__":
    sys.exit(main())
