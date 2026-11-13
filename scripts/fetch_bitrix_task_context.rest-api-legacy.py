"""Выгрузка контекста задачи Bitrix24: карточка, комменты форума, чат задачи и чаты из описания.

Портал/вебхук берётся из BITRIX_WEBHOOK_URL: --env <path>, иначе поиск .env вверх от cwd.

Запуск:
    python fetch_bitrix_task_context.py 123456
    python fetch_bitrix_task_context.py 123456 --out-dir data/bitrix --limit 500 --env /path/.env

Пишет:
    <out-dir>/task_<id>.json          - всё сырьё (задача, комменты, сообщения, users, files)
    <out-dir>/task_<id>.md            - карточка задачи + описание + список чатов
    <out-dir>/task_<id>_chat<N>.md    - читаемая переписка по каждому чату
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

DEFAULT_OUT_DIR = Path("data") / "bitrix"

TASK_SELECT = [
    "ID", "TITLE", "DESCRIPTION", "STATUS", "STAGE_ID", "GROUP_ID", "PARENT_ID", "CHAT_ID",
    "CREATED_BY", "RESPONSIBLE_ID", "ACCOMPLICES", "AUDITORS", "CREATED_DATE", "CHANGED_DATE",
    "DEADLINE", "CLOSED_DATE", "PRIORITY", "TAGS", "TIME_ESTIMATE", "MARK",
]

DIALOG_RE = re.compile(r"IM_DIALOG=(chat\d+|\d+)")
USER_BB_RE = re.compile(r"\[USER=(\d+)(?:\s+REPLACE)?\](.*?)\[/USER\]", re.S)
BB_RE = re.compile(r"\[/?[A-Za-z][^\]]*\]")
CHAT_SERVICE_CODES = {"CHAT_JOIN", "CHAT_LEAVE"}


def find_env_file(explicit: str | None = None) -> Path | None:
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.exists() else None
    for base in (Path.cwd(), *Path.cwd().parents):
        candidate = base / ".env"
        if candidate.exists():
            return candidate
    home_env = Path.home() / ".env"
    return home_env if home_env.exists() else None


def load_local_env(explicit: str | None = None) -> Path | None:
    path = find_env_file(explicit)
    if path is None:
        return None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
    return path


class BitrixRest:
    """Тонкая обёртка над REST Bitrix24: int/ORDER/start проходят без искажений."""

    def __init__(self, env_file: str | None = None) -> None:
        env_path = load_local_env(env_file)
        self.webhook = os.getenv("BITRIX_WEBHOOK_URL", "").strip()
        if not self.webhook:
            raise SystemExit(f"BITRIX_WEBHOOK_URL не найден (искал .env: {env_path or 'не найден'})")
        self.webhook = self.webhook.rstrip("/") + "/"
        self.session = requests.Session()

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        response = self.session.post(self.webhook + method + ".json", json=params or {}, timeout=60)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and "error" in data and "result" not in data:
            raise RuntimeError(f"{method}: {data.get('error')} — {data.get('error_description')}")
        return data

    def result(self, method: str, params: dict[str, Any] | None = None) -> Any:
        data = self.call(method, params)
        if isinstance(data, dict) and "result" in data:
            return data["result"]
        return data


def unwrap_batch(value: Any) -> Any:
    """Bitrix batch-ответы приходят как {'order0000000000': {...}} — разворачиваем."""
    if isinstance(value, dict) and len(value) == 1:
        key = next(iter(value))
        if isinstance(key, str) and key.startswith("order"):
            return value[key]
    return value


def normalize_list(value: Any, preferred: tuple[str, ...] = ()) -> list[Any]:
    value = unwrap_batch(value)
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in preferred:
            nested = value.get(key)
            if isinstance(nested, list):
                return nested
        for nested in value.values():
            if isinstance(nested, list):
                return nested
        return [value]
    return []


def clean_bb(text: str | None, users: dict[str, str] | None = None) -> str:
    text = text or ""
    users = users or {}

    def user_repl(match: re.Match[str]) -> str:
        uid, fallback = match.group(1), (match.group(2) or "").strip()
        return users.get(uid) or fallback or f"id{uid}"

    text = USER_BB_RE.sub(user_repl, text)
    text = BB_RE.sub("", text)
    text = text.replace("\r\n", "\n")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def load_users(rest: BitrixRest, ids: set[str]) -> dict[str, str]:
    ids_list = sorted({str(i) for i in ids if str(i or "").strip()})
    users: dict[str, str] = {}
    for offset in range(0, len(ids_list), 50):  # user.get: максимум 50 ID на запрос
        chunk = ids_list[offset:offset + 50]
        try:
            result = rest.result("user.get", {"FILTER": {"ID": chunk}})
        except Exception:  # noqa: BLE001 - имена не критичны
            continue
        for user in normalize_list(result):
            uid = str(user.get("ID") or "")
            name = " ".join(p for p in (user.get("NAME"), user.get("LAST_NAME")) if p).strip()
            users[uid] = name or user.get("EMAIL") or f"user {uid}"
    return users


def message_time(message: dict[str, Any]) -> str:
    return str(message.get("date") or message.get("DATE_CREATE") or "")


def author_name(message: dict[str, Any], users: dict[str, str]) -> str:
    aid = str(message.get("author_id") or message.get("AUTHOR_ID") or "")
    if aid in ("", "0"):
        return "— (система)"
    return f"{users.get(aid, 'неизвестный')} (id {aid})"


def extract_message_file_ids(message: dict[str, Any]) -> list[str]:
    params = message.get("params") or {}
    if isinstance(params, list):
        return []
    raw = params.get("FILE_ID") or params.get("FILE_IDS") or []
    if isinstance(raw, dict):
        return [str(v) for v in raw.values()]
    if isinstance(raw, (str, int)):
        return [str(raw)]
    return [str(v) for v in raw]


def fetch_chat_messages(rest: BitrixRest, dialog_id: str, limit: int) -> dict[str, Any]:
    """im.dialog.messages.get отдаёт сообщения от новых к старым; листаем через LAST_ID."""
    messages: list[dict[str, Any]] = []
    users: dict[str, str] = {}
    files: list[dict[str, Any]] = []
    chat_meta: dict[str, Any] = {}
    last_id: int | None = None
    error: str | None = None
    while len(messages) < limit:
        params: dict[str, Any] = {"DIALOG_ID": dialog_id, "LIMIT": min(50, limit - len(messages))}
        if last_id:
            params["LAST_ID"] = last_id
        try:
            raw = rest.result("im.dialog.messages.get", params)
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            break
        payload = unwrap_batch(raw)
        if not isinstance(payload, dict):
            error = f"неожиданный ответ: {str(payload)[:200]}"
            break
        chunk = normalize_list(payload.get("messages"), preferred=("messages",))
        chat_meta = {k: v for k, v in payload.items() if k not in ("messages", "users", "files")}
        for user in normalize_list(payload.get("users")):
            uid = str(user.get("id") or user.get("ID") or "")
            name = str(user.get("name") or "").strip()
            if uid and name:
                users[uid] = name
        files.extend(normalize_list(payload.get("files")))
        if not chunk:
            break
        messages.extend(chunk)
        ids = [int(m.get("id") or 0) for m in chunk if str(m.get("id") or "").isdigit()]
        if not ids:
            break
        new_last = min(ids)
        if new_last == last_id:
            break
        last_id = new_last
    messages.sort(key=lambda m: int(m.get("id") or 0))
    return {
        "dialog_id": dialog_id,
        "chat_meta": chat_meta,
        "messages": messages,
        "users": users,
        "files": files,
        "message_count": len(messages),
        "error": error,
    }


def sync_chat_users(rest: BitrixRest, chat: dict[str, Any]) -> None:
    known = dict(chat.get("users") or {})
    missing = {str(m.get("author_id") or "") for m in chat["messages"]} - set(known) - {"", "0", "None"}
    chat["users"] = {**load_users(rest, missing), **known}


def render_chat_md(task_id: str, chat: dict[str, Any], title: str) -> str:
    users = chat["users"]
    messages = chat["messages"]
    real = [
        m for m in messages
        if not (CHAT_SERVICE_CODES & {str(c) for c in ((m.get("params") or {}).get("CODE") or [])})
    ]
    lines = [
        f"# {title}", "",
        f"- Задача: {task_id}",
        f"- Диалог: {chat['dialog_id']}",
        f"- Сообщений: {len(messages)} (содержательных: {len(real)})",
    ]
    if messages:
        lines.append(f"- Период: {message_time(messages[0])} — {message_time(messages[-1])}")
    if chat.get("error"):
        lines.append(f"- Ошибка: {chat['error']}")
    lines.append("")
    for message in messages:
        codes = {str(c) for c in ((message.get("params") or {}).get("CODE") or [])}
        kind = " · системное" if codes & CHAT_SERVICE_CODES else ""
        lines += [
            f"### [{message_time(message)}] {author_name(message, users)}{kind}", "",
            clean_bb(message.get("text") or message.get("message"), users) or "(без текста)",
        ]
        file_ids = extract_message_file_ids(message)
        if file_ids:
            lines += ["", f"Вложения (file id): {', '.join(file_ids)}"]
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Выгрузка задачи Bitrix24: карточка, комменты, чаты.")
    parser.add_argument("task_id", help="ID задачи, напр. 123456")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--limit", type=int, default=500, help="максимум сообщений на чат")
    parser.add_argument("--max-chats", type=int, default=8, help="сколько чатов из описания тянуть")
    parser.add_argument("--env", default=None, help="путь к .env с BITRIX_WEBHOOK_URL")
    args = parser.parse_args()

    task_id = str(args.task_id).strip()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rest = BitrixRest(args.env)

    task = unwrap_batch(rest.result("tasks.task.get", {"taskId": int(task_id), "select": TASK_SELECT})) or {}
    if isinstance(task, dict) and "task" in task and not task.get("id"):
        task = task["task"]
    task = task if isinstance(task, dict) else {}

    legacy_error: str | None = None
    legacy_comments: list[Any] = []
    try:
        legacy_comments = normalize_list(
            rest.call("task.commentitem.getlist", {"TASKID": int(task_id), "ORDER": {"POST_DATE": "asc"}})
        )
    except Exception as exc:  # noqa: BLE001 - на большинстве порталов метод не работает, это не блокер
        legacy_error = f"{type(exc).__name__}: {exc}"

    user_ids = {
        str(task.get("createdBy") or ""),
        str(task.get("responsibleId") or ""),
        *(str(u) for u in (task.get("accomplices") or [])),
        *(str(u) for u in (task.get("auditors") or [])),
    }
    for comment in legacy_comments:
        if isinstance(comment, dict):
            user_ids.add(str(comment.get("AUTHOR_ID") or ""))
    users = load_users(rest, user_ids)

    description = task.get("description") or ""
    dialog_ids: list[str] = []
    task_chat_id = str(task.get("chatId") or "").strip()
    if task_chat_id:
        dialog_ids.append(f"chat{task_chat_id}")
    for match in DIALOG_RE.findall(description):
        dialog = match if match.startswith("chat") else f"chat{match}"
        if dialog not in dialog_ids:
            dialog_ids.append(dialog)
    dialog_ids = dialog_ids[: args.max_chats]

    chats: list[dict[str, Any]] = []
    for dialog in dialog_ids:
        chat = fetch_chat_messages(rest, dialog, args.limit)
        sync_chat_users(rest, chat)
        chats.append(chat)

    payload = {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "portal": rest.webhook.split("/rest/")[0],
        "task_id": task_id,
        "task": task,
        "users": users,
        "legacy_comments": legacy_comments,
        "legacy_comments_error": legacy_error,
        "dialog_ids": dialog_ids,
        "chats": chats,
    }
    json_path = out_dir / f"task_{task_id}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    def who(uid: Any) -> str:
        uid = str(uid or "")
        return f"{users.get(uid, 'неизвестный')} (id {uid})" if uid else "—"

    portal = payload["portal"]
    raw_description = clean_bb(description, users)
    lines = [
        f"# Задача Bitrix24 {task_id}: {task.get('title') or ''}", "",
        f"- Портал: {portal}",
        f"- Ссылка: {portal}/company/personal/user/{task.get('createdBy') or ''}/tasks/task/view/{task_id}/",
        f"- Выгружено: {payload['fetched_at']}",
        f"- Проект: {(task.get('group') or {}).get('name')} (группа {task.get('groupId')})",
        f"- Стадия/статус: {task.get('stageId')} / {task.get('status')}",
        f"- Постановщик: {who(task.get('createdBy'))}",
        f"- Ответственный: {who(task.get('responsibleId'))}",
        f"- Соисполнители: {', '.join(who(u) for u in (task.get('accomplices') or [])) or '—'}",
        f"- Наблюдатели: {', '.join(who(u) for u in (task.get('auditors') or [])) or '—'}",
        f"- Создана: {task.get('createdDate')} | изменена: {task.get('changedDate')} | дедлайн: {task.get('deadline')}",
        f"- Чат задачи: {('chat' + task_chat_id) if task_chat_id else '—'}", "",
        "## Описание", "", raw_description or "(пусто)", "",
    ]
    if description and description != raw_description:
        lines += ["### Описание (raw bbCode)", "", description, ""]
    lines += ["## Чаты", ""]
    for chat in chats:
        note = f" — ошибка: {chat['error']}" if chat.get("error") else ""
        lines.append(f"- {chat['dialog_id']}: {chat['message_count']} сообщений{note}")
    lines += ["", f"## Комментарии форума задачи ({len(legacy_comments)})", ""]
    if legacy_error:
        lines += [f"недоступны: {legacy_error}", ""]
    for index, comment in enumerate(legacy_comments, start=1):
        if not isinstance(comment, dict):
            continue
        lines += [
            f"### {index}. {who(comment.get('AUTHOR_ID'))} — {comment.get('POST_DATE')}", "",
            clean_bb(comment.get("POST_MESSAGE"), users) or "(без текста)", "",
        ]
    md_path = out_dir / f"task_{task_id}.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")

    titles = {f"chat{task_chat_id}": f"Чат задачи {task_id}"} if task_chat_id else {}
    chat_paths: list[Path] = []
    for chat in chats:
        title = titles.get(chat["dialog_id"], f"Чат {chat['dialog_id']} (из описания задачи {task_id})")
        path = out_dir / f"task_{task_id}_{chat['dialog_id']}.md"
        path.write_text(render_chat_md(task_id, chat, title), encoding="utf-8")
        chat_paths.append(path)

    print(f"Задача: {task.get('title')}")
    print(f"JSON:   {json_path}")
    print(f"MD:     {md_path}")
    for chat, path in zip(chats, chat_paths):
        print(f"Чат {chat['dialog_id']}: {chat['message_count']} сообщений -> {path}")
    if legacy_error:
        print(f"Комментарии форума недоступны: {legacy_error}")


if __name__ == "__main__":
    main()
