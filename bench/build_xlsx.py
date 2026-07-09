"""XLSX-корпус для эксперимента «REPL vs markdown-контекст».

Переиспользует детерминированный отбор build_corpus (seed=42): берёт те же
таблицы/вопросы, рендерит XLSX, пишет golden_xlsx.jsonl (~24 вопроса).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).parent))
from build_corpus import CORPUS, BENCH, load_wtq, gen_russian, safe_name  # noqa: E402

N_QUESTIONS = 24


def render_xlsx(title: str, cells: list[list[str]], path: Path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = (title or "data")[:28] or "data"
    for row in cells:
        ws.append([str(c) for c in row])
    wb.save(path)


def main():
    items = load_wtq() + gen_russian()
    golden, file_by_table = [], {}
    for it in items:
        if len(golden) >= N_QUESTIONS:
            break
        tid = it["table_id"]
        if tid not in file_by_table:
            idx = len(file_by_table)
            title = (
                tid.split("/")[-1].replace(".csv", "")
                if it["lang"] == "en" else tid.split("-")[1]
            )
            fname = f"x{idx:03d}-{safe_name(str(title))}.xlsx"
            render_xlsx(str(title), it["table"], CORPUS / fname)
            file_by_table[tid] = fname
        golden.append({
            "qid": f"x{len(golden):03d}", "file": file_by_table[tid],
            "fmt": "xlsx", "lang": it["lang"],
            "question": it["question"], "answer": it["answer"],
        })

    out = BENCH / "golden_xlsx.jsonl"
    with open(out, "w") as f:
        for g in golden:
            f.write(json.dumps(g, ensure_ascii=False) + "\n")
    langs = {}
    for g in golden:
        langs[g["lang"]] = langs.get(g["lang"], 0) + 1
    print(f"xlsx-файлов: {len(file_by_table)}, вопросов: {len(golden)}, языки: {langs}")


if __name__ == "__main__":
    main()
