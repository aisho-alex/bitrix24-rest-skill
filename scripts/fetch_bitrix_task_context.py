#!/usr/bin/env python3
"""Bitrix24 через REST-вебхук: контекст задачи, чаты, вложения + список чатов и непрочитанные.

Портативно: вебхук ищется в --env / $BITRIX_WEBHOOK_URL / ./.env / ~/.bitrix.env /
~/.config/bitrix/webhook.env

Задача и её обсуждение:
    python3 fetch_bitrix_task_context.py 123456
    python3 fetch_bitrix_task_context.py --url "https://<portal>/company/personal/user/<uid>/tasks/task/view/123456/"
    python3 fetch_bitrix_task_context.py 123456 --out-dir ./bitrix --limit 1000
    python3 fetch_bitrix_task_context.py 123456 --download-files
    python3 fetch_bitrix_task_context.py --dialog chat222222 --dialog chat333333

Чаты и непрочитанное:
    python3 fetch_bitrix_task_context.py --chats 40            # последние чаты (счётчик непрочитанных)
    python3 fetch_bitrix_task_context.py --unread              # только непрочитанные + сводка im.counters
    python3 fetch_bitrix_task_context.py --find "договор"      # поиск чатов по названию
    python3 fetch_bitrix_task_context.py --info chat222222      # карточка чата: участники, кто до чего прочитал

Зависимости: requests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("Нужен пакет requests: python3 -m pip install requests")

DEFAULT_OUT_DIR = Path.cwd() / "bitrix_export"

TASK_SELECT = [
    "ID", "TITLE", "DESCRIPTION", "STATUS", "STAGE_ID", "GROUP_ID", "PARENT_ID", "CHAT_ID",
    "CREATED_BY", "RESPONSIBLE_ID", "ACCOMPLICES", "AUDITORS", "CREATED_DATE", "CHANGED_DATE",
    "DEADLINE", "CLOSED_DATE", "PRIORITY", "TAGS", "TIME_ESTIMATE", "MARK",
]

DIALOG_RE = re.compile(r"IM_DIALOG=(chat\d+|\d+)")
TASK_URL_RE = re.compile(r"/tasks/task/view/(\d+)")
WEBHOOK_USER_RE = re.compile(r"/rest/(\d+)/")
USER_BB_RE = re.compile(r"\[USER=(\d+)(?:\s+REPLACE)?\](.*?)\[/USER\]", re.S)
BB_RE = re.compile(r"\[/?[A-Za-z][^\]]*\]")
CHAT_SERVICE_CODES = {"CHAT_JOIN", "CHAT_LEAVE"}

COUNTER_TYPES = ("ALL", "MESSENGER", "CHAT", "TASKS_TASK", "COLLAB", "DIALOG", "NOTIFY", "LINES", "COPILOT")
COUNTER_LISTS = (
    "CHAT_UNREAD", "DIALOG_UNREAD", "TASKS_TASK_UNREAD", "COLLAB_UNREAD",
    "COPILOT_UNREAD", "CHANNEL_COMMENT",
)

ENV_CANDIDATES = (
    Path("./.env"),
    Path("~/.bitrix.env").expanduser(),
    Path("~/.config/bitrix/webhook.env").expanduser(),
)


# ----------------------------------------------------------------------------- webhook / http


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def resolve_webhook(explicit: str | None, env_file: str | None) -> tuple[str, str]:
    if explicit:
        return explicit.strip(), "--webhook-url"
    if env_file:
        values = parse_env_file(Path(env_file).expanduser())
        url = values.get("BITRIX_WEBHOOK_URL", "")
        if not url:
            sys.exit(f"BITRIX_WEBHOOK_URL не найден в {env_file}")
        return url, env_file
    if os.getenv("BITRIX_WEBHOOK_URL", "").strip():
        return os.environ["BITRIX_WEBHOOK_URL"].strip(), "$BITRIX_WEBHOOK_URL"
    for candidate in ENV_CANDIDATES:
        url = parse_env_file(candidate).get("BITRIX_WEBHOOK_URL", "")
        if url:
            return url, str(candidate)
    sys.exit(
        "BITRIX_WEBHOOK_URL не найден. Задайте переменную окружения, положите .env рядом "
        "или запустите с --webhook-url/--env."
    )


def redact(text: str, webhook: str) -> str:
    token = webhook.rstrip("/").split("/")[-1]
    if token:
        text = text.replace(token, "<TOKEN>")
    return text


class BitrixRest:
    def __init__(self, webhook: str) -> None:
        self.webhook = webhook.rstrip("/") + "/"
        self.session = requests.Session()
        match = WEBHOOK_USER_RE.search(self.webhook)
        self.user_id = match.group(1) if match else ""

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        try:
            response = self.session.post(self.webhook + method + ".json", json=params or {}, timeout=60)
            response.raise_for_status()
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            detail = f"HTTP {status}" if status else type(exc).__name__
            # сообщение requests содержит URL с токеном вебхука — не светим его
            raise RuntimeError(redact(f"{method}: {detail} (запрос отклонён порталом)", self.webhook)) from None
        data = response.json()
        if isinstance(data, dict) and "error" in data and "result" not in data:
            raise RuntimeError(redact(f"{method}: {data.get('error')} — {data.get('error_description')}", self.webhook))
        return data

    def result(self, method: str, params: dict[str, Any] | None = None) -> Any:
        data = self.call(method, params)
        return data["result"] if isinstance(data, dict) and "result" in data else data

    def paged(self, method: str, params: dict[str, Any] | None = None, max_items: int = 5000) -> list[Any]:
        """Постраничный обход методов *.list ('start'/'next')."""
        items: list[Any] = []
        start: Any = 0
        while True:
            page_params = {**(params or {}), "start": start}
            payload = self.call(method, page_params)
            result = payload.get("result", payload) if isinstance(payload, dict) else payload
            chunk = normalize_list(result)
            items.extend(chunk)
            next_start = payload.get("next") if isinstance(payload, dict) else None
            if not next_start or len(items) >= max_items or not chunk:
                break
            start = next_start
        return items[:max_items]


def unwrap_batch(value: Any) -> Any:
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


# ----------------------------------------------------------------------------- текст


def clean_bb(text: str | None, users: dict[str, str] | None = None) -> str:
    text = text or ""
    users = users or {}

    def user_repl(match: re.Match[str]) -> str:
        uid, fallback = match.group(1), (match.group(2) or "").strip()
        return users.get(uid) or fallback or f"id{uid}"

    text = USER_BB_RE.sub(user_repl, text)
    text = BB_RE.sub("", text)
    text = text.replace("\r\n", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def load_users(rest: BitrixRest, ids: set[str]) -> dict[str, str]:
    ids_list = sorted({str(i) for i in ids if str(i or "").strip()})
    users: dict[str, str] = {}
    for offset in range(0, len(ids_list), 50):
        try:
            result = rest.result("user.get", {"FILTER": {"ID": ids_list[offset:offset + 50]}})
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


def collect_chat_attachments(chat: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = {
        str(item.get("id")): item
        for item in chat.get("files") or []
        if isinstance(item, dict) and item.get("id") is not None
    }
    attachments: dict[str, dict[str, Any]] = {}
    for message in chat["messages"]:
        message_id = message.get("id")
        for file_id in dict.fromkeys(extract_message_file_ids(message)):
            file_meta = metadata.get(file_id) or {}
            raw_size = file_meta.get("size")
            try:
                expected_size = int(raw_size) if raw_size is not None else None
            except (TypeError, ValueError):
                expected_size = None
            attachment = attachments.setdefault(file_id, {
                "file_id": file_id,
                "name": str(file_meta.get("name") or f"file_{file_id}"),
                "expected_size": expected_size,
                "message_ids": [],
            })
            if message_id is not None and message_id not in attachment["message_ids"]:
                attachment["message_ids"].append(message_id)
    return list(attachments.values())


def safe_attachment_filename(file_id: str, name: str) -> str:
    clean = re.sub(r"[\\/]+", "_", str(name).replace("\x00", "")).strip()
    clean = re.sub(r"[^\w.() -]+", "_", clean, flags=re.UNICODE)
    clean = re.sub(r"\s+", "_", clean).strip(" ._") or f"file_{file_id}"
    return f"{file_id}_{clean[:180]}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_attachment(
    rest: BitrixRest,
    url: str,
    target: Path,
    expected_size: int | None,
) -> tuple[int, str]:
    portal = rest.webhook.split("/rest/")[0] + "/"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "*/*",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Referer": portal,
    }
    part = target.with_name(target.name + ".part")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with rest.session.get(url, headers=headers, stream=True, allow_redirects=True, timeout=300) as response:
            if response.status_code != 200:
                raise RuntimeError(f"HTTP {response.status_code} при скачивании файла")
            content_type = (response.headers.get("Content-Type") or "").lower()
            disposition = (response.headers.get("Content-Disposition") or "").lower()
            is_attachment = "attachment" in disposition
            if ("text/html" in content_type or "json" in content_type) and not is_attachment:
                raise RuntimeError(f"вместо файла получен ответ {content_type.split(';', 1)[0]}")
            digest = hashlib.sha256()
            size = 0
            with part.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
        if expected_size is not None and size != expected_size:
            raise RuntimeError(f"размер файла {size} не совпадает с метаданными {expected_size}")
        part.replace(target)
        return size, digest.hexdigest()
    except requests.RequestException as exc:
        part.unlink(missing_ok=True)
        status = getattr(getattr(exc, "response", None), "status_code", None)
        detail = f"HTTP {status}" if status else type(exc).__name__
        raise RuntimeError(f"ошибка скачивания: {detail}") from None
    except Exception:
        part.unlink(missing_ok=True)
        raise


def download_chat_files(
    rest: BitrixRest,
    chat: dict[str, Any],
    out_dir: Path,
) -> tuple[dict[str, dict[str, Any]], Path]:
    dialog_id = chat["dialog_id"]
    files_dir = out_dir / "files" / dialog_id
    results: dict[str, dict[str, Any]] = {}
    manifest_files: list[dict[str, Any]] = []
    for attachment in collect_chat_attachments(chat):
        file_id = attachment["file_id"]
        target = files_dir / safe_attachment_filename(file_id, attachment["name"])
        result: dict[str, Any] = {
            "file_id": file_id,
            "name": attachment["name"],
            "message_ids": attachment["message_ids"],
            "path": target.relative_to(out_dir).as_posix(),
        }
        try:
            expected_size = attachment["expected_size"]
            if target.is_file() and expected_size is not None and target.stat().st_size == expected_size:
                result.update(status="skipped", size=expected_size, sha256=sha256_file(target))
            else:
                response = rest.result("im.v2.File.download", {
                    "dialogId": dialog_id,
                    "fileId": int(file_id) if file_id.isdigit() else file_id,
                })
                url = (response or {}).get("downloadUrl") if isinstance(response, dict) else None
                if not url:
                    raise RuntimeError("im.v2.File.download не вернул downloadUrl")
                size, digest = download_attachment(rest, str(url), target, expected_size)
                result.update(status="success", size=size, sha256=digest)
        except Exception as exc:  # noqa: BLE001 - ошибка одного файла не отменяет выгрузку чата
            result.update(status="error", error=str(exc))
        results[file_id] = result
        manifest_files.append(result)

    files_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = files_dir / "manifest.json"
    manifest = {
        "dialog_id": dialog_id,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "files": manifest_files,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return results, manifest_path


# ----------------------------------------------------------------------------- чаты: список / непрочитанные

TYPE_RU = {
    "chat": "чат",
    "tasksTask": "чат задачи",
    "calendar": "чат встречи",
    "open": "открытый канал",
    "openChannel": "канал",
    "collab": "коллаба",
    "sonetGroup": "чат проекта",
    "call": "звонок",
    "crm": "CRM",
    "support24": "линия",
    "bot": "бот",
}


def normalize_recent(item: dict[str, Any]) -> dict[str, Any]:
    chat = item.get("chat") or {}
    message = item.get("message") or {}
    entity_link = chat.get("entity_link") or {}
    return {
        "dialog_id": str(item.get("id") or (f"chat{chat.get('id')}" if chat.get("id") else "")),
        "chat_id": chat.get("id") or item.get("chat_id"),
        "title": item.get("title") or chat.get("name") or "(без названия)",
        "type": chat.get("type") or item.get("type") or "",
        "entity_type": chat.get("entity_type") or "",
        "entity_id": str(chat.get("entity_id") or ""),
        "unread": bool(item.get("unread")),
        "counter": int(item.get("counter") or 0),
        "last_message_id": item.get("last_id") or chat.get("last_id"),
        "date_update": item.get("date_update") or item.get("date_last_activity") or "",
        "last_author_id": str(message.get("author_id") or ""),
        "last_text": message.get("text") or "",
        "last_date": message.get("date") or "",
        "members": chat.get("user_counter"),
        "message_count": chat.get("message_count"),
        "entity_url": entity_link.get("url") or "",
        "role": chat.get("role") or "",
    }


def fetch_recent(rest: BitrixRest, limit: int, skip_openlines: bool = True) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"LIMIT": min(limit, 50)}
    if skip_openlines:
        params["SKIP_OPENLINES"] = "Y"
    items = normalize_list(rest.result("im.recent.get", params), preferred=("items",))
    return [normalize_recent(item) for item in items if isinstance(item, dict)][:limit]


def fetch_counters(rest: BitrixRest) -> dict[str, Any]:
    try:
        return rest.result("im.counters.get", {}) or {}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def search_chats(rest: BitrixRest, query: str) -> list[dict[str, Any]]:
    items = normalize_list(rest.result("im.search.chat.list", {"FIND": query}))
    found: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        found.append({
            "dialog_id": f"chat{item.get('id')}" if item.get("id") else "",
            "chat_id": item.get("id"),
            "title": item.get("name") or "(без названия)",
            "type": item.get("type") or "",
            "entity_type": item.get("entity_type") or "",
            "entity_id": str(item.get("entity_id") or ""),
            "message_count": item.get("message_count"),
            "counter": int(item.get("counter") or 0),
            "entity_url": (item.get("entity_link") or {}).get("url") or "",
        })
    return found


def dialog_candidates(value: str) -> list[str]:
    """chat222222 / 222222 → ['chat222222'];  2156 (личка/бот) → ['chat2156', '2156']."""
    value = str(value).strip()
    if value.startswith("chat"):
        return [value]
    if value.isdigit():
        return [f"chat{value}", value]
    return [value]


def fetch_dialog_info(rest: BitrixRest, value: str) -> dict[str, Any]:
    """Карточка чата. Для личных диалогов (DIALOG_ID = id пользователя) im.dialog.get отдаёт 403."""
    info: dict[str, Any] = {"dialog_id": value}
    errors: list[str] = []
    for candidate in dialog_candidates(value):
        chat_id = candidate[4:] if candidate.startswith("chat") else candidate
        try:
            chat = unwrap_batch(rest.result("im.dialog.get", {"DIALOG_ID": candidate}))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{candidate}: {type(exc).__name__}: {exc}")
            chat = None
        if chat:
            info["dialog_id"] = candidate
            info["chat"] = chat
            break
    info.setdefault("chat", None)
    if info.get("chat") is None:
        info["chat_error"] = "; ".join(errors) or "карточка чата не получена"
        info["dialog_id"] = value
    try:
        info["users"] = normalize_list(unwrap_batch(rest.result("im.dialog.users.list", {"DIALOG_ID": info["dialog_id"]})))
    except Exception as exc:  # noqa: BLE001
        info["users"] = []
        info["users_error"] = f"{type(exc).__name__}: {exc}"
    return info


def read_state(info: dict[str, Any], my_id: str) -> dict[str, Any]:
    """Что известно про прочтение: readed_list чата + счётчик из im.recent.get."""
    chat = info.get("chat") or {}
    last_id = chat.get("last_message_id") or chat.get("last_id")
    state: dict[str, Any] = {"my_id": my_id, "last_message_id": last_id, "read_by_me_to": None, "me_read": None, "counter": None}
    for entry in normalize_list(chat.get("readed_list")):
        if str(entry.get("user_id")) == str(my_id):
            state["read_by_me_to"] = entry.get("message_id")
            state["read_at"] = entry.get("date")
            try:
                state["me_read"] = int(entry.get("message_id") or 0) >= int(last_id or 0)
            except (TypeError, ValueError):
                state["me_read"] = None
    return state


def recent_for_dialog(rest: BitrixRest, dialog_id: str) -> dict[str, Any] | None:
    try:
        for item in fetch_recent(rest, limit=50):
            if item["dialog_id"] == dialog_id:
                return item
    except Exception:  # noqa: BLE001
        return None
    return None


def link_for(item: dict[str, Any], portal: str) -> str:
    if item.get("entity_url"):
        url = str(item["entity_url"])
        return url if url.startswith("http") else portal + url
    return f"{portal}/online/?IM_DIALOG={item.get('dialog_id', '')}"


def render_chats_md(items: list[dict[str, Any]], counters: dict[str, Any], portal: str, title: str, users: dict[str, str]) -> str:
    lines = [f"# {title}", "", f"- Портал: {portal}", f"- Сформировано: {datetime.now().isoformat(timespec='seconds')}", f"- Чатов: {len(items)}", ""]
    if counters and not counters.get("error"):
        types = counters.get("TYPE") or {}
        summary = ", ".join(f"{name}={types.get(name, 0)}" for name in COUNTER_TYPES if types.get(name))
        lines += ["## Счётчики непрочитанного (im.counters.get)", "", summary or "всё прочитано (0)", ""]
        for key in COUNTER_LISTS:
            values = counters.get(key) or []
            if values:
                lines.append(f"- {key}: {json.dumps(values, ensure_ascii=False)}")
        lines.append("")
    lines += ["## Чаты", "", "| диалог | непрочит. | тип | название | участников | последнее сообщение | когда | ссылка |",
              "|---|---|---|---|---|---|---|---|"]
    for item in items:
        author = users.get(item["last_author_id"], f"id{item['last_author_id']}" if item["last_author_id"] else "—")
        text = clean_bb(item["last_text"], users).replace("\n", " ")[:140]
        lines.append(
            f"| {item['dialog_id']} | {item['counter'] or ''} | {TYPE_RU.get(item['type'], item['type'])} | {item['title'][:60]} | "
            f"{item['members'] or ''} | {author}: {text} | {item['last_date'] or item['date_update']} | {link_for(item, portal)} |"
        )
    lines.append("")
    return "\n".join(lines)


# ----------------------------------------------------------------------------- чаты: сообщения


def fetch_chat_messages(rest: BitrixRest, dialog_id: str, limit: int) -> dict[str, Any]:
    """im.dialog.messages.get отдаёт от новых к старым; листаем в прошлое через LAST_ID."""
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
            payload = unwrap_batch(rest.result("im.dialog.messages.get", params))
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            break
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
        ids = [int(m.get("id")) for m in chunk if str(m.get("id") or "").isdigit()]
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
    missing = {str(m.get("author_id") or "") for m in chat["messages"]} - set(known)
    missing -= {"", "0", "None"}
    chat["users"] = {**load_users(rest, missing), **known}


def render_chat_md(
    task_id: str,
    chat: dict[str, Any],
    title: str,
    attachments: dict[str, dict[str, Any]] | None = None,
) -> str:
    users, messages = chat["users"], chat["messages"]
    real = [m for m in messages if not (CHAT_SERVICE_CODES & {str(c) for c in ((m.get("params") or {}).get("CODE") or [])})]
    lines = [
        f"# {title}",
        "",
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
        lines.append(f"### [{message_time(message)}] {author_name(message, users)}{kind}")
        lines.append("")
        lines.append(clean_bb(message.get("text") or message.get("message"), users) or "(без текста)")
        file_ids = extract_message_file_ids(message)
        if file_ids and attachments is None:
            lines += ["", f"Вложения (file id): {', '.join(file_ids)}"]
        elif file_ids:
            lines += ["", "Вложения:"]
            for file_id in dict.fromkeys(file_ids):
                attachment = (attachments or {}).get(file_id)
                if not attachment:
                    lines.append(f"- file id {file_id}")
                    continue
                name = str(attachment["name"]).replace("[", "\\[").replace("]", "\\]")
                if attachment["status"] in ("success", "skipped"):
                    lines.append(
                        f"- [{name}]({attachment['path']}) "
                        f"(file id {file_id}, {attachment['size']} байт; {attachment['status']})"
                    )
                else:
                    error = str(attachment.get("error") or "неизвестная ошибка").replace("\n", " ")
                    lines.append(f"- {name} (file id {file_id}) — ошибка выгрузки: {error}")
        lines.append("")
    return "\n".join(lines)


def render_dialog_info_md(info: dict[str, Any], state: dict[str, Any], portal: str) -> str:
    chat = info.get("chat") or {}
    dialog_id = info["dialog_id"]
    lines = [
        f"# Чат {dialog_id}: {chat.get('name') or '(без названия)'}",
        "",
        f"- Портал: {portal}",
        f"- Ссылка: {portal}/online/?IM_DIALOG={dialog_id}",
        f"- Тип: {TYPE_RU.get(chat.get('type') or '', chat.get('type') or '—')}",
        f"- Сущность: {chat.get('entity_type') or '—'} {chat.get('entity_id') or ''}",
        f"- Владелец (id): {chat.get('owner') or '—'}; моя роль: {chat.get('role') or '—'}",
        f"- Участников: {chat.get('user_counter') or '—'}; сообщений: {chat.get('message_count') or '—'}",
        f"- Создан: {chat.get('date_create') or '—'}; последнее сообщение id: {state.get('last_message_id') or chat.get('last_message_id') or '—'}",
        "",
    ]
    if info.get("chat_error") and not info.get("chat"):
        lines += [f"Ошибка загрузки карточки чата: {info['chat_error']}", ""]
    my_id = state.get("my_id")
    counter = state.get("counter")
    if state.get("read_by_me_to"):
        verdict = "прочитано до последнего сообщения" if state.get("me_read") else "ЕСТЬ НЕПРОЧИТАННЫЕ (я не дочитал)"
        lines.append(
            f"- Прочтение (я, id {my_id}): до сообщения {state['read_by_me_to']} ({state.get('read_at') or 'дата неизвестна'}) — {verdict}"
        )
    elif counter is not None:
        verdict = f"ЕСТЬ НЕПРОЧИТАННЫЕ ({counter})" if counter > 0 else "прочитано (счётчик 0)"
        lines.append(f"- Прочтение (я, id {my_id}): счётчик непрочитанного {counter} — {verdict}")
    else:
        lines.append(
            f"- Прочтение (я, id {my_id}): в readed_list меня нет, и чата нет в im.recent.get — "
            "статус не определить (readed_list в Bitrix не включает текущего пользователя вебхука)"
        )
    lines.append("")
    readed = normalize_list(chat.get("readed_list"))
    if readed:
        lines += [f"## Кто до чего прочитал ({len(readed)})", ""]
        for entry in readed:
            lines.append(f"- {entry.get('user_name') or entry.get('user_id')} (id {entry.get('user_id')}): до {entry.get('message_id')} ({entry.get('date')})")
        lines.append("")
    users = info.get("users") or []
    if users:
        lines += [f"## Участники ({len(users)})", ""]
        for user in users:
            status = user.get("status") or ("бот" if user.get("bot") else "")
            lines.append(f"- {user.get('name')} (id {user.get('id')}) — {user.get('work_position') or '—'} [{status}]")
        lines.append("")
    return "\n".join(lines)


# ----------------------------------------------------------------------------- main


def write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    print(f"-> {path}")


def run_chats_mode(args: argparse.Namespace, rest: BitrixRest, portal: str, out_dir: Path) -> None:
    if args.find:
        found = search_chats(rest, args.find)
        print(f"Найдено чатов по «{args.find}»: {len(found)}")
        for item in found:
            print(f"  {item['dialog_id']:>14}  непрочит={item['counter'] or 0:<3} [{TYPE_RU.get(item['type'], item['type'])}] "
                  f"{item['title'][:70]}  ({item['message_count'] or '?'} сообщ.)  {link_for(item, portal)}")
        write_text(out_dir / f"chats_find_{re.sub(r'[^A-Za-z0-9а-яА-Я]+', '_', args.find)}.json",
                   json.dumps({"query": args.find, "found": found}, ensure_ascii=False, indent=2, default=str))
        return

    if args.info:
        info = fetch_dialog_info(rest, args.info)
        state = read_state(info, rest.user_id)
        recent = recent_for_dialog(rest, info["dialog_id"]) or recent_for_dialog(rest, str(args.info).strip())
        if recent:
            state["counter"] = recent["counter"]
            state["last_message_id"] = state["last_message_id"] or recent["last_message_id"]
            if not info.get("chat"):
                info["chat_source"] = "im.recent.get"
                info["chat"] = {
                    "name": recent["title"], "type": recent["type"], "entity_type": recent["entity_type"],
                    "entity_id": recent["entity_id"], "user_counter": recent["members"],
                    "message_count": recent["message_count"], "last_message_id": recent["last_message_id"],
                    "role": recent["role"], "readed_list": [],
                }
        print(render_dialog_info_md(info, state, portal))
        write_text(out_dir / f"{info['dialog_id']}_info.json",
                   json.dumps({"info": info, "read_state": state, "recent": recent}, ensure_ascii=False, indent=2, default=str))
        return

    items = fetch_recent(rest, limit=args.chats or (50 if args.unread else 30))
    if args.unread:
        items = [item for item in items if item["counter"] > 0 or item["unread"]]
    counters = fetch_counters(rest)
    user_ids = {item["last_author_id"] for item in items}
    users = load_users(rest, user_ids)

    types = (counters.get("TYPE") or {}) if isinstance(counters, dict) else {}
    summary = ", ".join(f"{name}={types.get(name, 0)}" for name in COUNTER_TYPES if types.get(name)) or "всё прочитано"
    print(f"Непрочитанное (im.counters.get): {summary}")
    for key in COUNTER_LISTS:
        values = (counters.get(key) or []) if isinstance(counters, dict) else []
        if values:
            print(f"  {key}: {json.dumps(values, ensure_ascii=False)}")
    print(f"Чатов в выдаче: {len(items)}" + (" (только непрочитанные)" if args.unread else ""))
    print()
    header = f"{'диалог':>14}  {'непр':>4}  {'тип':<12}  название"
    print(header)
    for item in items:
        author = users.get(item["last_author_id"], item["last_author_id"] or "—")
        text = clean_bb(item["last_text"], users).replace("\n", " ")[:90]
        print(f"{item['dialog_id']:>14}  {item['counter'] or 0:>4}  {TYPE_RU.get(item['type'], item['type'])[:12]:<12}  {item['title'][:70]}")
        print(f"{'':>14}  {'':>4}  {item['last_date'] or item['date_update']}  {author}: {text}")
        print(f"{'':>14}  {'':>4}  {link_for(item, portal)}")
    title = "Непрочитанные чаты Bitrix24" if args.unread else "Последние чаты Bitrix24"
    md = render_chats_md(items, counters, portal, title, users)
    write_text(out_dir / ("chats_unread.md" if args.unread else "chats_recent.md"), md)
    write_text(out_dir / ("chats_unread.json" if args.unread else "chats_recent.json"),
               json.dumps({"counters": counters, "items": items}, ensure_ascii=False, indent=2, default=str))


def run_task_mode(args: argparse.Namespace, rest: BitrixRest, portal: str, out_dir: Path, task_id: str) -> None:
    task: dict[str, Any] = {}
    if task_id != "0":
        task = unwrap_batch(rest.result("tasks.task.get", {"taskId": int(task_id), "select": TASK_SELECT})) or {}
        if isinstance(task, dict) and "task" in task and not task.get("id"):
            task = task["task"]
        task = task if isinstance(task, dict) else {}

    legacy_comments: list[Any] = []
    legacy_error: str | None = None
    if task_id != "0":
        try:
            legacy_comments = normalize_list(
                rest.call("task.commentitem.getlist", {"TASKID": int(task_id), "ORDER": {"POST_DATE": "asc"}})
            )
        except Exception as exc:  # noqa: BLE001
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
    for dialog in args.dialog:
        dialog = dialog.strip()
        if dialog and dialog not in dialog_ids:
            dialog_ids.append(dialog)
    dialog_ids = dialog_ids[: max(args.max_chats, len(args.dialog))]

    chats: list[dict[str, Any]] = []
    for dialog in dialog_ids:
        chat = fetch_chat_messages(rest, dialog, args.limit)
        sync_chat_users(rest, chat)
        chats.append(chat)

    payload = {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "portal": portal,
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

    attachments_by_dialog: dict[str, dict[str, dict[str, Any]]] = {}
    manifest_paths: dict[str, Path] = {}
    if args.download_files:
        for chat in chats:
            attachments, manifest_path = download_chat_files(rest, chat, out_dir)
            attachments_by_dialog[chat["dialog_id"]] = attachments
            manifest_paths[chat["dialog_id"]] = manifest_path

    def who(uid: Any) -> str:
        uid = str(uid or "")
        return f"{users.get(uid, 'неизвестный')} (id {uid})" if uid else "—"

    lines = [
        f"# Задача Bitrix24 {task_id}: {task.get('title') or '(без карточки)'}",
        "",
        f"- Портал: {portal}",
        f"- Ссылка: {portal}/company/personal/user/{task.get('createdBy') or ''}/tasks/task/view/{task_id}/",
        f"- Выгружено: {payload['fetched_at']} (источник вебхука: {args._webhook_source})",
        f"- Проект: {(task.get('group') or {}).get('name') or '—'} (группа {task.get('groupId') or '—'})",
        f"- Стадия/статус: {task.get('stageId')} / {task.get('status')}",
        f"- Постановщик: {who(task.get('createdBy'))}",
        f"- Ответственный: {who(task.get('responsibleId'))}",
        f"- Соисполнители: {', '.join(who(u) for u in (task.get('accomplices') or [])) or '—'}",
        f"- Наблюдатели: {', '.join(who(u) for u in (task.get('auditors') or [])) or '—'}",
        f"- Создана: {task.get('createdDate')} | изменена: {task.get('changedDate')} | дедлайн: {task.get('deadline')}",
        f"- Чат задачи: {('chat' + task_chat_id) if task_chat_id else '—'}",
        "",
        "## Описание",
        "",
        clean_bb(description, users) or "(пусто)",
        "",
    ]
    if clean_bb(description, users) != description.strip():
        lines += ["### Описание (raw bbCode)", "", description, ""]
    lines += ["## Чаты", ""]
    for chat in chats:
        suffix = f" — ошибка: {chat['error']}" if chat.get("error") else ""
        lines.append(f"- {chat['dialog_id']}: {chat['message_count']} сообщений{suffix}")
    lines += ["", f"## Комментарии форума задачи ({len(legacy_comments)})", ""]
    if legacy_error:
        lines += [f"недоступны: {legacy_error}", ""]
    for index, comment in enumerate(legacy_comments, start=1):
        if not isinstance(comment, dict):
            continue
        lines += [
            f"### {index}. {who(comment.get('AUTHOR_ID'))} — {comment.get('POST_DATE')}",
            "",
            clean_bb(comment.get("POST_MESSAGE"), users) or "(без текста)",
            "",
        ]
    md_path = out_dir / f"task_{task_id}.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")

    chat_paths: list[Path] = []
    titles = {f"chat{task_chat_id}": f"Чат задачи {task_id}"} if task_chat_id else {}
    for chat in chats:
        title = titles.get(chat["dialog_id"], f"Чат {chat['dialog_id']} (из описания задачи {task_id})")
        path = out_dir / f"task_{task_id}_{chat['dialog_id']}.md"
        attachments = attachments_by_dialog.get(chat["dialog_id"]) if args.download_files else None
        path.write_text(render_chat_md(task_id, chat, title, attachments), encoding="utf-8")
        chat_paths.append(path)

    print(f"Задача: {task.get('title') or '(карточка не загружена)'}")
    print(f"JSON:   {json_path}")
    print(f"MD:     {md_path}")
    for chat, path in zip(chats, chat_paths):
        warn = f"  [ОШИБКА: {chat['error']}]" if chat.get("error") else ""
        print(f"Чат {chat['dialog_id']}: {chat['message_count']} сообщений -> {path}{warn}")
        if args.download_files:
            attachments = attachments_by_dialog[chat["dialog_id"]]
            counts = {
                status: sum(item["status"] == status for item in attachments.values())
                for status in ("success", "skipped", "error")
            }
            print(
                f"Файлы {chat['dialog_id']}: скачано {counts['success']}, "
                f"пропущено {counts['skipped']}, ошибок {counts['error']} -> "
                f"{manifest_paths[chat['dialog_id']]}"
            )
    if legacy_error:
        print(f"Комментарии форума недоступны: {legacy_error}")
    if any(chat["message_count"] >= args.limit for chat in chats):
        print("ВНИМАНИЕ: упёрлись в --limit, история может быть обрезана — поднять лимит.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bitrix24: контекст задачи (карточка/чаты) + список чатов и непрочитанное."
    )
    parser.add_argument("task_id", nargs="?", help="ID задачи, напр. 123456")
    parser.add_argument("--url", help="Ссылка на задачу — ID возьмётся из неё")
    parser.add_argument("--dialog", action="append", default=[], help="Дополнительный чат (chat222222); можно повторять")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--limit", type=int, default=1000, help="максимум сообщений на чат")
    parser.add_argument("--download-files", action="store_true", help="Скачать вложения чатов и записать manifest")
    parser.add_argument("--max-chats", type=int, default=8, help="сколько чатов из описания задачи тянуть")
    parser.add_argument("--env", help="Файл с BITRIX_WEBHOOK_URL")
    parser.add_argument("--webhook-url", help="Вебхук целиком (перекрывает .env)")
    parser.add_argument("--chats", type=int, nargs="?", const=30, help="Показать последние чаты (по умолчанию 30)")
    parser.add_argument("--unread", action="store_true", help="Только непрочитанные чаты + сводка счётчиков")
    parser.add_argument("--find", help="Поиск чатов по названию (im.search.chat.list)")
    parser.add_argument("--info", help="Карточка чата: участники, прочтение, тип (--info chat222222)")
    args = parser.parse_args()

    if isinstance(args.chats, int) and args.chats <= 0:
        args.chats = 30

    chat_mode = bool(args.chats or args.unread or args.find or args.info)

    task_id = (args.task_id or "").strip()
    if not task_id and args.url:
        match = TASK_URL_RE.search(args.url)
        task_id = match.group(1) if match else ""
    if not task_id and not args.dialog and not chat_mode:
        parser.error("Укажите task_id, --url, --chats/--unread/--find/--info или --dialog")
    task_id = task_id or "0"

    webhook, source = resolve_webhook(args.webhook_url, args.env)
    rest = BitrixRest(webhook)
    args._webhook_source = source  # type: ignore[attr-defined]
    portal = webhook.split("/rest/")[0]
    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Вебхук: {source} (портал {portal}, пользователь вебхука id {rest.user_id or '?'})\n")

    if chat_mode:
        run_chats_mode(args, rest, portal, out_dir)
    if task_id != "0" or args.dialog:
        run_task_mode(args, rest, portal, out_dir, task_id)


if __name__ == "__main__":
    main()
