"""Agentic-бенчмарк GigaAgent: вложения с таблицами, A/B = markitdown off/on.

Гоняет вопросы из golden.jsonl через ЖИВОЙ агент (upload → thread →
runs/wait c auto_approve) и пишет json-строки результатов в bench/results/.

Запуск (условие определяется тем, с каким GIGA_AGENT_MARKITDOWN поднят бэк):
    .venv/bin/python bench/agentic_bench.py --condition A --limit 30 --repeats 3
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

BASE = "http://localhost:8123/api"
BENCH = Path(__file__).parent
RESULTS = BENCH / "results"
RESULTS.mkdir(exist_ok=True)

ADMIN = ("admin@example.com", "giga_agent_admin")
ANSWER_SUFFIX = (
    "\n\nОтветь МАКСИМАЛЬНО кратко: только само значение (число или слово), "
    "без пояснений."
)


def login(client: httpx.Client) -> str:
    r = client.post(f"{BASE}/agent/auth/token",
                    data={"username": ADMIN[0], "password": ADMIN[1]})
    r.raise_for_status()
    return r.json()["access_token"]


def upload(client: httpx.Client, tok: str, path: Path) -> dict:
    with open(path, "rb") as f:
        r = client.post(
            f"{BASE}/agent/files/upload",
            headers={"Authorization": f"Bearer {tok}"},
            files={"file": (path.name, f)},
            data={"file_type": "other"},
        )
    r.raise_for_status()
    p = r.json()
    return {
        "path": p.get("path") or p.get("sandbox_path") or "",
        "original_name": p.get("original_name") or path.name,
        "file_type": p.get("file_type") or "other",
        "size": int(p.get("size") or 0),
    }


def run_agent(client: httpx.Client, tok: str, question: str, file_payload: dict) -> dict:
    """Один агентский ран: тред → runs/wait → финальное состояние."""
    h = {"Authorization": f"Bearer {tok}"}
    tid = client.post(f"{BASE}/threads", headers=h, json={}).json()["thread_id"]
    content = question + ANSWER_SUFFIX
    body = {
        "assistant_id": "giga_agent",
        "input": {
            "messages": [{
                "type": "human",
                "content": content,
                "additional_kwargs": {
                    "user_input": content,
                    "files": [file_payload],
                },
            }],
            "collections": [],
            "mcp_tools": [],
        },
        "config": {"configurable": {"auto_approve": True}},
    }
    t0 = time.time()
    r = client.post(f"{BASE}/threads/{tid}/runs/wait", headers=h, json=body,
                    timeout=300.0)
    latency = time.time() - t0
    r.raise_for_status()
    values = r.json()
    if isinstance(values, dict) and "values" in values:
        values = values["values"]
    messages = values.get("messages", []) if isinstance(values, dict) else []
    ai = [m for m in messages if m.get("type") == "ai"]
    tool_calls = sum(len(m.get("tool_calls") or []) for m in ai)
    answer = ""
    for m in reversed(ai):
        c = m.get("content")
        if isinstance(c, str) and c.strip():
            answer = c.strip()
            break
    return {
        "thread_id": tid, "answer": answer, "latency_s": round(latency, 1),
        "n_ai_messages": len(ai), "n_tool_calls": tool_calls,
        "n_messages": len(messages),
    }


def tokens_between(t_start: str, t_end: str) -> dict:
    q = (
        "SELECT coalesce(sum(input_tokens),0), coalesce(sum(output_tokens),0), count(*) "
        f"FROM core_usage_events WHERE created_at >= '{t_start}' AND created_at <= '{t_end}';"
    )
    out = subprocess.run(
        ["docker", "exec", "giga_agent_dev-giga-agent-postgres-1",
         "psql", "-U", "postgres", "-d", "postgres", "-tAc", q],
        capture_output=True, text=True,
    ).stdout.strip()
    i, o, n = (out.split("|") + ["0", "0", "0"])[:3]
    return {"input_tokens": int(i or 0), "output_tokens": int(o or 0),
            "model_calls": int(n or 0)}


_norm_re = re.compile(r"[^\w\.\-]+", re.UNICODE)


def normalize(s: str) -> str:
    return _norm_re.sub(" ", (s or "").lower().strip()).strip()


def is_correct(gold: str, answer: str) -> bool:
    g, a = normalize(gold), normalize(answer)
    if not g or not a:
        return False
    if g == a:
        return True
    # число: сравнить как float (учёт "5", "5.0", "5,0")
    try:
        return abs(float(g.replace(",", "."))
                   - float(a.replace(",", ".").split()[0])) < 1e-6
    except (ValueError, IndexError):
        pass
    # короткий текстовый эталон допускаем вхождением
    return f" {g} " in f" {a} "


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f%z")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--condition", required=True)  # произвольная метка условия
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--only-qids", default=None, help="через запятую")
    ap.add_argument("--golden", default=None, help="путь к golden jsonl (дефолт bench/golden.jsonl)")
    args = ap.parse_args()

    golden_path = Path(args.golden) if args.golden else BENCH / "golden.jsonl"
    golden = [json.loads(l) for l in open(golden_path)]
    if args.only_qids:
        keep = set(args.only_qids.split(","))
        golden = [g for g in golden if g["qid"] in keep]
    else:
        # стратифицировано: чередуем срезы, чтобы limit покрыл все
        golden.sort(key=lambda g: (g["qid"]))
        by_slice: dict = {}
        for g in golden:
            by_slice.setdefault((g["fmt"], g["lang"]), []).append(g)
        mixed = []
        while any(by_slice.values()) and len(mixed) < args.limit:
            for k in list(by_slice):
                if by_slice[k] and len(mixed) < args.limit:
                    mixed.append(by_slice[k].pop(0))
        golden = mixed

    out_path = RESULTS / f"cond{args.condition}.jsonl"
    done = set()
    if out_path.exists():
        for l in open(out_path):
            r = json.loads(l)
            done.add((r["qid"], r["repeat"]))

    client = httpx.Client(timeout=300.0)
    tok = login(client)
    print(f"условие {args.condition}: вопросов {len(golden)} × {args.repeats} повторов; уже сделано {len(done)}")

    with open(out_path, "a") as out:
        for g in golden:
            fpath = BENCH / "corpus" / g["file"]
            for rep in range(args.repeats):
                if (g["qid"], rep) in done:
                    continue
                rec = None
                for attempt in range(2):  # ретрай на транзиентные сбои песочницы
                    try:
                        fp = upload(client, tok, fpath)
                        t_start = now_utc()
                        res = run_agent(client, tok, g["question"], fp)
                        time.sleep(1.2)  # дать fire-and-forget usage-записи доехать
                        t_end = now_utc()
                        usage = tokens_between(t_start, t_end)
                        answer_l = (res["answer"] or "").lower()
                        transient = (
                            "система занята" in answer_l
                            or "лимит виртуальных" in answer_l
                        )
                        if transient and attempt == 0:
                            print(f"  {g['qid']} r{rep}: транзиентный сбой песочницы, ретрай через 25с")
                            time.sleep(25)
                            continue
                        rec = {
                            "qid": g["qid"], "repeat": rep, "fmt": g["fmt"],
                            "lang": g["lang"], "gold": g["answer"],
                            "correct": is_correct(g["answer"], res["answer"]),
                            "transient": transient,
                            **res, **usage,
                        }
                        break
                    except Exception as e:  # не роняем прогон из-за одного рана
                        if attempt == 0:
                            time.sleep(25)
                            continue
                        rec = {"qid": g["qid"], "repeat": rep, "fmt": g["fmt"],
                               "lang": g["lang"], "gold": g["answer"],
                               "error": f"{type(e).__name__}: {e}"[:200],
                               "correct": False}
                time.sleep(2.5)  # межрановая пауза — не дёргать lifecycle песочницы
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                out.flush()
                mark = "✓" if rec.get("correct") else ("✗" if "error" not in rec else "E")
                print(f"  {g['qid']} r{rep} [{g['fmt']}/{g['lang']}] {mark} "
                      f"{rec.get('latency_s','-')}s calls={rec.get('model_calls','-')} "
                      f"tok={rec.get('input_tokens',0)+rec.get('output_tokens',0)}")
    print("done:", out_path)


if __name__ == "__main__":
    main()
