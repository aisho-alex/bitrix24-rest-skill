#!/usr/bin/env python3
"""Дельта по выгрузке задачи Bitrix24: что прибавилось с прошлого запуска.

Зачем: скрипт выгрузки (fetch_bitrix_task_context.py) перезаписывает файлы на месте и
истории дельт не ведёт. Этот скрипт держит точку отсчёта в sidecar-файле и на
следующем запуске печатает только новое.

Использование:
    python3 task_delta.py <out-dir>                  # сводка + новые сообщения
    python3 task_delta.py <out-dir> --task 123456    # если в каталоге несколько задач
    python3 task_delta.py <out-dir> --reset          # сбросить точку отсчёта (baseline)
    python3 task_delta.py <out-dir> --full           # печатать новые целиком, не обрезая

Типичный цикл «актуализируй»:
    python3 task_delta.py <dir>                      # 1. что есть сейчас / что нового
    python3 .../fetch_bitrix_task_context.py <id> --out-dir <dir> --limit 1000
    python3 task_delta.py <dir>                      # 2. дельта после выгрузки
    python3 .../fetch_bitrix_task_context.py ... ; python3 task_delta.py <dir>  # baseline сдвинут
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

MSG_RE = re.compile(r"^### \[([^\]]+)\]\s*(.*)$")
COUNT_RE = re.compile(r"^- Сообщений:\s*(\d+)")
PERIOD_RE = re.compile(r"^- Период:\s*(\S+)\s+—\s+(\S+)")
MODIFIED_RE = re.compile(r"изменена:\s*([0-9T:+\-]+)")
STATE_NAME = ".delta_state.json"


def parse_ts(value: str):
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def parse_chat(path: Path):
    """-> dict(count, first_ts, last_ts, messages=[(ts_str, header, body)])"""
    text = path.read_text(encoding="utf-8", errors="replace")
    count = None
    first_ts = last_ts = None
    messages = []
    cur = None
    for line in text.splitlines():
        m = COUNT_RE.match(line)
        if m and count is None:
            count = int(m.group(1))
            continue
        p = PERIOD_RE.match(line)
        if p and first_ts is None:
            first_ts, last_ts = p.group(1), p.group(2)
            continue
        h = MSG_RE.match(line)
        if h:
            if cur:
                messages.append(cur)
            cur = [h.group(1), h.group(2).strip(), []]
            continue
        if cur is not None:
            stripped = line.rstrip()
            if stripped and not stripped.startswith("-"):
                cur[2].append(stripped)
    if cur:
        messages.append(cur)
    if last_ts is None and messages:
        first_ts, last_ts = messages[0][0], messages[-1][0]
    return {
        "count": count if count is not None else len(messages),
        "first_ts": first_ts,
        "last_ts": last_ts,
        "messages": [(t, h, "\n".join(b).strip()) for t, h, b in messages],
    }


def newer_than(messages, ref):
    """Сообщения строго новее ref (по datetime, с откатом на строковое сравнение)."""
    if not ref:
        return list(messages)
    ref_dt = parse_ts(ref)
    out = []
    for ts, head, body in messages:
        ts_dt = parse_ts(ts)
        if ref_dt is not None and ts_dt is not None:
            if ts_dt > ref_dt:
                out.append((ts, head, body))
        elif ts > ref:
            out.append((ts, head, body))
    return out


def clip(body: str, limit: int) -> str:
    body = re.sub(r"^-{10,}$", "", body, flags=re.M).strip()
    body = re.sub(r"\n{3,}", "\n\n", body)
    if limit and len(body) > limit:
        return body[:limit].rstrip() + " […]"
    return body


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    ap.add_argument("--task", help="id задачи (иначе берётся единственная найденная)")
    ap.add_argument("--reset", action="store_true", help="перезаписать точку отсчёта")
    ap.add_argument("--full", action="store_true", help="не обрезать тела сообщений")
    ap.add_argument("--body-limit", type=int, default=900)
    args = ap.parse_args()

    root = Path(args.out_dir).expanduser().resolve()
    if not root.is_dir():
        print(f"нет каталога: {root}", file=sys.stderr)
        return 2

    tasks = {}
    for p in sorted(root.glob("task_*_chat*.md")):
        m = re.match(r"task_(\d+)_chat(\d+)\.md$", p.name)
        if m:
            tasks.setdefault(m.group(1), {})[m.group(2)] = p
    if not tasks:
        print(f"в {root} нет файлов вида task_<id>_chat<NNN>.md", file=sys.stderr)
        return 2
    task_id = args.task or (list(tasks)[0] if len(tasks) == 1 else None)
    if task_id is None:
        print(f"задач несколько ({', '.join(tasks)}), укажи --task", file=sys.stderr)
        return 2
    if task_id not in tasks:
        print(f"нет выгрузки по задаче {task_id}", file=sys.stderr)
        return 2

    state_path = root / STATE_NAME
    state = {}
    if state_path.exists() and not args.reset:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
    if args.reset:
        state = {}
    chats_state = state.get("chats", {})
    baseline = not chats_state

    card = root / f"task_{task_id}.md"
    modified = None
    if card.exists():
        mod = MODIFIED_RE.search(card.read_text(encoding="utf-8", errors="replace"))
        modified = mod.group(1) if mod else None
    prev_modified = state.get("modified")

    print(f"# Дельта по задаче {task_id} ({root})")
    if baseline:
        print("# точки отсчёта не было — ставлю её сейчас, дельта появится со следующего запуска")
    if modified and prev_modified and modified != prev_modified:
        print(f"# карточка изменена: {prev_modified} -> {modified}")
    elif modified and not baseline:
        print(f"# карточка (изменена): {modified} (без изменений с прошлого запуска)")

    total_new = 0
    for chat, path in sorted(tasks[task_id].items(), key=lambda kv: int(kv[0])):
        info = parse_chat(path)
        prev = chats_state.get(chat, {})
        ref = None if baseline else prev.get("last_ts")
        new = []
        if not baseline:
            new = newer_than(info["messages"], ref)
        total_new += len(new)
        grew = info["count"] - prev.get("count", info["count"]) if prev else 0
        head = f"== chat{chat}: {info['count']} сообщений"
        if not baseline:
            head += f", +{len(new)} новых" if new else " — нового нет"
        print(head)
        if baseline:
            print(f"   период: {info['first_ts']} — {info['last_ts']}")
        for ts, who, body in new:
            print(f"\n### [{ts}] {who}")
            print(clip(body, 0 if args.full else args.body_limit))
        chats_state[chat] = {"count": info["count"], "last_ts": info["last_ts"]}

    if not baseline and total_new == 0:
        print("\n# вывод: с прошлой выгрузки новых сообщений нет "
              "(рост другого чата не считай ошибкой — чаты живут порознь)")

    state = {"task": task_id, "chats": chats_state, "modified": modified}
    try:
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
    except OSError as exc:
        print(f"# не смог записать {state_path}: {exc}", file=sys.stderr)
    print(f"\n# точка отсчёта: {state_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
