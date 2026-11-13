#!/usr/bin/env python3
"""Показать сообщения чата из выгрузки Bitrix24 новее указанного момента.

Применение: «актуализируй» — надо отдать только дельту с прошлой выгрузки.
Cutoff берётся из строки `- Выгружено: <локальное время>` прошлой шапки task_<id>.md.
Таймстампы сообщений в портале идут со своим офсетом (+04:00), поэтому строки
сравнивать нельзя — сравниваются абсолютные моменты времени.

usage:
    delta_since.py <task_<id>_chat<N>.md> <2026-09-18T11:45:35> [--append <файл>]

Время cutoff без офсета трактуется как локальное (как в шапке выгрузки).
Вывод: блоки «### [ts] Автор» с текстом, в исходном порядке, плюс счётчик в stderr.
"""
from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path

HEAD = re.compile(r"^### \[([^\]]+)\]")
SEP = re.compile(r"^-{10,}$")


def parse_cutoff(raw: str) -> dt.datetime:
    stamp = dt.datetime.fromisoformat(raw)
    if stamp.tzinfo is None:
        stamp = stamp.astimezone()  # наивное время = локальное
    return stamp


def parse_stamp(raw: str) -> dt.datetime | None:
    raw = raw.strip()
    # в системных строках после таймстампа идёт "— (система)"
    try:
        stamp = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.astimezone()
    return stamp


def clean(block: list[str]) -> str:
    """Убрать строки-разделители цитат, оставить текст."""
    out = [ln for ln in block if not SEP.match(ln.strip())]
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out).strip()


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    path = Path(argv[1])
    cutoff = parse_cutoff(argv[2])
    out_path = None
    if "--append" in argv:
        out_path = Path(argv[argv.index("--append") + 1])

    blocks: list[tuple[dt.datetime, str, str]] = []
    cur_stamp: dt.datetime | None = None
    cur_head = ""
    cur_body: list[str] = []

    def flush() -> None:
        if cur_stamp is not None and cur_stamp > cutoff:
            blocks.append((cur_stamp, cur_head, clean(cur_body)))

    for line in path.read_text(encoding="utf-8").splitlines():
        m = HEAD.match(line)
        if m:
            flush()
            cur_stamp = parse_stamp(m.group(1).split("]")[0].replace("—", "").strip())
            cur_head = line
            cur_body = []
            continue
        if cur_stamp is not None:
            cur_body.append(line)
    flush()

    text = "\n\n".join(f"{head}\n\n{body}" for _, head, body in blocks)
    print(text)
    tail = f"[delta] новее {cutoff.isoformat()}: {len(blocks)} сообщ."
    print(tail, file=sys.stderr)
    if out_path is not None:
        out_path.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
