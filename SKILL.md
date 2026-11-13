---
name: bitrix24-rest-api
description: "Bitrix24 umbrella — read/write portal data over the REST webhook: tasks and their chats/comments, kanban stages, users, disk files, downloading chat attachments, plus the chat-list/unread digest and the task export + actualization (delta) workflow. Portal quirks, pagination, bbCode decoding, raw-REST wrapper, and the ready export scripts."
tags: [bitrix24, rest, webhook, tasks, chat, crm, bitrix24-mcp]
---

# Bitrix24 REST API (webhook)

Use when the task mentions Bitrix24 / Битрикс24, a `*.bitrix24.*` portal URL
(e.g. `https://<portal>/company/personal/user/<uid>/tasks/task/view/<id>/`),
`BITRIX_WEBHOOK_URL`, or pulling task comments / chat context / kanban state out of Bitrix.

## 0. Credentials first (do not ask the user)

A webhook is created in Bitrix24 by a portal admin:
*Рабочее пространство → Разработчикам → Другое → Входящий вебхук* — scope needs
`task`, `im`, `user`; `im` is sufficient for downloading chat attachments. It looks like

    BITRIX_WEBHOOK_URL=https://<portal>/rest/<userId>/<token>/

Keep it in an env file — the script looks for it in this order: `--env <file>` /
`$BITRIX_WEBHOOK_URL` → `./.env` → `~/.bitrix.env` → `~/.config/bitrix/webhook.env`.

- Never echo the token into chat; mask it when printing diagnostics:
  `sed -E 's#(https://[^ ]*/rest/[0-9]+/)[A-Za-z0-9]+#\1<SECRET>#'`.
- The numeric segment is the **webhook owner** (the user the webhook was created by),
  independent of the `user/<uid>` segment in a task URL. Don't match them up.
- Read-only REST calls against a SaaS portal are fine without asking.

## 1. Call over raw REST, not fast_bitrix24

`fast_bitrix24` is convenient for app code but for ad-hoc extraction it fights you:

| Symptom | Cause |
|---|---|
| `ViolationError: get_all() doesn't support parameters 'start' or 'order'` | `bx.get_all(method, params={\"ORDER\": ...})` — ORDER is not allowed there |
| `WRONG_ARGUMENTS ... Param #0 (taskId) ... expected to be of type \"integer\"` | `bx.call(..., {\"TASKID\": 123, \"ORDER\": {...}})` — batching mangles the int |
| `call(): item_list can't be empty` | `bx.call(\"im.recent.get\", {})` / `bx.call(\"profile\")` — empty dict item rejected |
| `[UserWarning] It's better to use get_all()...` | noise from `bx.call` on `*.list` / `*.getlist` |

Use a ~20-line `requests` wrapper instead — ints, `ORDER`, and `start` survive
untouched (full class in `scripts/fetch_bitrix_task_context.py`):

```python
class BitrixRest:
    def __init__(self):  # webhook from env
        self.webhook = os.environ["BITRIX_WEBHOOK_URL"].rstrip("/") + "/"
        self.session = requests.Session()
    def call(self, method, params=None):
        r = self.session.post(self.webhook + method + ".json", json=params or {}, timeout=60)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "error" in data and "result" not in data:
            raise RuntimeError(f"{method}: {data['error']} — {data.get('error_description')}")
        return data
    def result(self, method, params=None):
        data = self.call(method, params)
        return data["result"] if isinstance(data, dict) and "result" in data else data
```

Keeps any app's own `fast_bitrix24` path untouched — the two coexist fine.

## 2. Unwrap and paginate

- Single-param calls can come back wrapped in a Bitrix batch key:
  `{"order0000000000": {...}}`. Always `unwrap_batch()` before reading — otherwise
  you get `{}`-looking task cards and silently-empty results.
- `*.list` methods: loop on the `start` param, read `next` from the top-level payload,
  accumulate `result.tasks`. `next == -1` / absent → done.
- `im.dialog.messages.get`: **newest first**, `LIMIT` caps at 50, page with
  `LAST_ID = min(id seen so far)`. Stop when a page returns no messages or `last_id`
  does not advance. Sort ascending locally before rendering.
- `task.commentitem.getlist` is unreliable on cloud portals (often always
  `WRONG_ARGUMENTS`, even with a real int). In current Bitrix the task discussion lives
  in the **task chat**, so this method returning nothing is normally the correct, complete
  answer — not a bug to keep fighting. Record it as a note in the output instead of retrying.

## 3. Where a task's discussion actually is

1. `tasks.task.get` with `select` including **`CHAT_ID`** → the task's own chat, dialog id
   `chat<chatId>`. This is the real "comments" feed (system events + people talking).
2. Task description often links extra chats: regex `IM_DIALOG=(chat\d+|\d+)` over
   `description` → `https://…/online/?IM_DIALOG=chatNNNNNN`.
3. Pull all of them: task chat first, then description-linked chats. Reflected example:
   a task whose `chatId` is `chat111111`, plus `chat222222` (main discussion) and
   `chat333333` (tech meeting) linked from the description — all three are context.

`tasks.task.get` also returns `creator`, `responsible`, `group`, `accomplices`,
`auditors` as nested objects with names — no separate `user.get` needed for the card.
For message authors use `im.dialog.messages.get`'s own `users` array, then `user.get`
with `{"FILTER": {"ID": [ids]}}` (max 50 ids per call) for the leftovers.

## 4. Render like a human

- bbCode: `[USER=669 REPLACE]Иванов Иван[/USER]` → `Иванов Иван`
  (`[USER=(\d+)(?:\s+REPLACE)?\](.*?)\[/USER\]`), then strip residual tags
  `\[/?[A-Za-z][^\]]*\]`. Keep the raw bbCode in a collapsed section if it differs.
- Mark service events (`params.CODE` ∈ `{CHAT_JOIN, CHAT_LEAVE}`, `author_id == 0`)
  as «системное» and count "содержательных" separately.
- Write both artefacts: raw JSON (trail for later analysis) + Markdown transcript
  (what a human actually reads). Header should carry period, counts, names, links.

## 5. Ready script

The script and its modes are described in §Task export and chats below.

`references/task-chat-export.md` — verbatim error transcripts, response shapes, and a
worked example.

## 6. «Что нового в сообщениях» (дайджест непрочитанного)

`im.recent.get` items carry `id` — and it may already be **prefixed**: on current cloud
portals it is a string like `"chat222222"`, with the numeric `chat_id` next to it.
If you blindly add the type (`f"chat{id}"`) you get `chatchat222222`
→ `403 Forbidden` (chat) or `400 DIALOG_ID_EMPTY`. Rule: if `id` starts with
`chat`/`user` — use it as is; otherwise prefix by `type`. Then `counter > 0` = unread, and
`im.counters.get` gives the portal totals (`TYPE.ALL`, `CHAT_MUTED`, `*_UNREAD`).
Working shape: `im.recent.get {"SKIP_OPENLINES": "Y"}` → sort `date` desc →
pull 50 msgs per chat → filter locally by date for "today"/"yesterday".

## §Установка

1. Скопируйте каталог скилла в `~/.hermes/skills/` (Hermes) или в `./skills/` проекта —
   структура `SKILL.md` + `scripts/` + `references/` самодостаточна.
2. `python3 -m pip install requests` (или свой venv: `python3 -m venv venv && . venv/bin/activate`).
3. Положить вебхук в `~/.bitrix.env`:
   `echo 'BITRIX_WEBHOOK_URL=https://<portal>/rest/<userId>/<token>/' > ~/.bitrix.env`
   и `chmod 600 ~/.bitrix.env`.
4. Проверка: `python3 scripts/fetch_bitrix_task_context.py --chats 5` — должен вернуть
   список последних чатов, а не `KeyError: BITRIX_WEBHOOK_URL`.

## §Task export and chats (готовый скрипт)

`scripts/fetch_bitrix_task_context.py` — один скрипт покрывает и выгрузку задачи,
и режимы чатов. Флаговые режимы (список чатов, непрочитанные, поиск, карточка чата)
**заменяют** прежний отдельный путь — это одна команда:

```bash
# задача + её чаты
python3 scripts/fetch_bitrix_task_context.py 123456
... 123456 --out-dir ./bitrix --limit 1000          # куда писать / сколько сообщений на чат
... 123456 --download-files                          # скачать вложения чатов + manifest
... --dialog chat222222 --dialog chat333333          # только конкретные чаты
... --webhook-url "https://portal/rest/1/token/"     # другой портал/вебхук

# чаты и непрочитанное
... --chats 40            # последние 40 чатов со счётчиками непрочитанного
... --unread              # только непрочитанные + сводка im.counters.get
... --find "договор"      # поиск чатов по названию
... --info chat222222     # карточка чата: участники, кто до чего прочитал, счётчик
```

Зависимости: только `requests`.

Вебхук ищется в первом найденном из: `--env <файл>` / `$BITRIX_WEBHOOK_URL` →
`./.env` → `~/.bitrix.env` → `~/.config/bitrix/webhook.env`. Токен вебхука в чат/лог
не печатать — в скрипте есть `redact()`; при ручном curl — маскировать.

**Что получается:**

```
<out-dir>/task_<id>.json             всё сырьё: карточка, users, комменты, сообщения, meta чатов
<out-dir>/task_<id>.md               карточка задачи + описание (чистый текст и raw bbCode) + список чатов
<out-dir>/task_<id>_chat<NNN>.md     читаемая переписка: [дата] Автор (id) → текст, вложения
<out-dir>/files/<dialog>/<fileId>_<имя>  скачанные вложения (при --download-files)
<out-dir>/files/<dialog>/manifest.json   статусы, пути, размеры, sha256 и ошибки вложений
<out-dir>/chats_recent.{md,json}     последние чаты: счётчик, тип, участники, последнее сообщение, ссылка
<out-dir>/chats_unread.{md,json}     то же, только непрочитанные + сводка im.counters.get
<out-dir>/chats_find_<запрос>.json   результат поиска чатов по названию
<out-dir>/<dialog>_info.json         карточка чата + состояние прочтения + участники
```

В markdown: bbCode вычищен, `[USER=id]` заменены на имена, системные сообщения
(вход/выход из чата) помечены, указаны период и число содержательных сообщений.

**Где брать каждый факт:**

| Что нужно | Метод | Особенности |
|---|---|---|
| список чатов + счётчик непрочитанного + последнее сообщение | `im.recent.get` (`LIMIT`, `SKIP_OPENLINES=Y`) | `counter`, `unread`, `chat.type`, `chat.entity_type/entity_id`, `entity_link.url` (для чата задачи — прямая ссылка на задачу) |
| сводка непрочитанного по типам | `im.counters.get` (пустое тело) | `TYPE` (ALL/MESSENGER/CHAT/TASKS_TASK/…), списки `CHAT_UNREAD`, `TASKS_TASK_UNREAD` (там `{"<chatId>": N}`) |
| карточка чата | `im.dialog.get` (`DIALOG_ID`) | имя, тип, владелец, участники, `message_count`, `last_message_id`, `readed_list` |
| кто до чего прочитал | `readed_list` в `im.dialog.get` | НЕ включает текущего пользователя вебхука — своё прочтение смотреть по `counter` |
| участники | `im.dialog.users.list` | с должностями и статусом online |
| файлы чата | `im.dialog.messages.get` → `im.v2.File.download` (`dialogId`, `fileId`) → GET `downloadUrl` | `FILE_ID` и метаданные уже есть в истории; `downloadUrl` одноразовый, не логировать |
| поиск по названию | `im.search.chat.list` (`FIND=`) | находит чаты/встречи/каналы, но НЕ чаты задач (у них имя = имя задачи, в поиск не попадают) |

Виды чатов: `tasksTask` — чат задачи (entity `TASKS_TASK <taskId>`), `sonetGroup` —
чат проекта, `calendar` — чат встречи (entity `CALENDAR <eventId>`), `private` — личка/бот
(`DIALOG_ID` = id пользователя, БЕЗ префикса `chat`).

У непрочитанного **два независимых сигнала**, оба выводятся: `counter` из
`im.recent.get` (сколько не прочитано именно у пользователя вебхука) и
`im.counters.get` (агрегат по порталу). Если расходятся — верить `counter` по
конкретному чату.

### §Проверка результата (обязательно перед «готово»)

- В `task_<id>.md` есть заголовок, постановщик/ответственный, число чатов.
- Период каждого чата начинается с его первого сообщения, а не «обрезан» на дату
  создания задачи — если `message_count` совпал с `--limit`, история обрезана:
  увеличить `--limit` и перезапустить.
- В чатах-обсуждениях есть не только системные события (`CHAT_JOIN`/`CHAT_LEAVE`/«создал
  задачу») — если содержательных 0, скорее всего взят не тот диалог или у вебхука нет
  доступа.
- Файлы непустые и не содержат `null` вместо текста.
- Для `--download-files`: сверить manifest с числом уникальных `FILE_ID`; у каждого `success`/`skipped`
  должны быть существующий файл, размер и sha256. Ошибки одного вложения не отменяют остальную выгрузку.
  В Markdown должны быть относительные локальные ссылки, а не подписанные `downloadUrl`.
- Для `--unread`: сумма по чатам должна согласовываться с `im.counters.get`; если в
  выдаче 0 непрочитанных, но `TYPE.ALL > 0` — увеличить `--chats` (счётчик может быть
  у чата, который не попал в первые N) либо смотреть `*_UNREAD`-списки в JSON.
- Для `--info`: если карточка чата не получена (403 на личке), статус прочтения берётся
  из `counter`; писать в ответе, откуда данные, а не «прочитано» наугад.

### §Актуализация («актуализируй из <ссылка>») — повторная выгрузка и отчёт о дельте

Триггер: в директории уже лежит история по задаче (`task_<id>*.md/json`), просят
«актуализировать» или дают ту же ссылку второй раз. Смысл просьбы — не «выгрузи
заново», а **что появилось нового**. Скрипт перезаписывает файлы на месте, поэтому
дельту надо готовить **ДО** запуска.

1. **Снять точку отсчёта до запуска** (иначе дельта потеряется):
   ```bash
   cd <dir> && for f in *.md; do echo "== $f"; head -8 "$f"; wc -l "$f"; done
   ```
   Нужны: число сообщений (`Сообщений: N`) и конец периода (`Период: ... — <TS>`)
   по каждому чату, плюс строка «изменена» из `task_<id>.md`. Ещё вариант —
   `cp` файлов в `/tmp` и потом `diff`.
2. Перезапустить скрипт той же командой с `--out-dir .` и `--limit 1000`.
3. Разницу считать по числу сообщений и по последнему TS прошлой выгрузки, **не по
   строке «Выгружено»** (TZ-грабли: в выгрузке локальное время, в сообщениях —
   время портала со смещением). Новое = заголовки `### [TS]` с TS строго больше
   прошлого конца периода:
   ```bash
   python3 scripts/delta_since.py task_<id>_chat<NNN>.md 2026-09-18T11:45:35
   awk '/^### \[<прошлый_последний_TS>/{f=1} f' task_<id>_chat<NNN>.md
   ```
   При большом количестве сообщений резать вывод (`head -300`) и добирать хвост
   вторым `awk` — иначе тихо потеряешь последние сообщения, а они обычно самые важные.
4. Проверить карточку и состав контекста: изменилась ли строка «изменена», стадия/статус,
   списки соисполнителей/наблюдателей; не появились ли в новых сообщениях ссылок на
   **другие** чаты:
   ```bash
   grep -ohE 'IM_DIALOG=[A-Za-z0-9]+' *.md | sort | uniq -c
   grep -oE 'chat[0-9]{5,7}' *.md | sort | uniq -c
   ```
   Нашлись новые диалоги — добирать точечно через `--dialog chat<NNN>`.
5. Перечитать и сгруппировать новое **по темам, а не по порядку сообщений**. В отчёте
   отдельным блоком — то, что кто-то обязался сделать и к какому сроку («обещанные
   сроки»), и то, где нужен сам пользователь/его линия. Сообщения самого пользователя
   вебхука стоит помечать явно: по ним спрашивают «а что ответили на мой вопрос».
6. Формат отчёта: простой текст для терминала (без markdown-заголовков и таблиц),
   по-русски, абсолютными путями к файлам, с указанием, сколько сообщений прибавилось
   и в каких чатах. Чаты без нового — перечислить одной строкой с их «тишиной с <дата>»:
   это тоже ответ.

**Готовый помощник вместо ручного awk:** `scripts/task_delta.py <out-dir> [--task <id>] [--reset]`
— держит точку отсчёта в `<out-dir>/.delta_state.json` (первый запуск ставит baseline,
дальше печатает только новые сообщения по каждому чату и изменение строки «изменена»).
Цикл: `task_delta.py <dir>` → выгрузка → `task_delta.py <dir>`.
При первом использовании сверить его вывод с ручным рецептом на одном чате.

Детальный рецепт и шаблон отчёта о дельте: `references/actualization-and-delta-report.md`.

### §MCP-сервер (для агентов: Hermes, opencode, Claude Desktop)

Отдельный MCP-обёртка над тем же клиентом (read-only, FastMCP, stdio или HTTP):

```
bitrix_mcp/client.py   REST-клиент (retry, кэш имён/проектов, маскировка токена, LAST_ID-листание)
bitrix_mcp/render.py   рендер в компактный markdown
bitrix_mcp/server.py   FastMCP: 10 инструментов, stdio (default) + --transport http --port
tools/register_opencode_mcp.py   прописать/убрать сервер в ~/.config/opencode/opencode.json
tests/smoke_mcp_client.py        смоук-тест через настоящий MCP-клиент
```

Инструменты: `bitrix_whoami`, `bitrix_find_projects`, `bitrix_project_context` (основной —
описание проекта, стадии, задачи по свежести, опционально переписка чатов задач),
`bitrix_task`, `bitrix_task_search`, `bitrix_chats`, `bitrix_chat_info`,
`bitrix_chat_messages` (со `since_id` для инкрементального добора), `bitrix_search`,
`bitrix_users`.

```bash
python3 -m bitrix_mcp.server --check      # портал + список инструментов
python3 tests/smoke_mcp_client.py         # боевые вызовы, ждём «провалов 0»
python3 tools/register_opencode_mcp.py   # прописать сервер в opencode
```

Конфиг opencode: `mcp.<name> = {type: "local", command: [...], cwd, environment, enabled, timeout}` —
`timeout` ставить 45000+, дефолтные 5000 мс рвут выгрузку истории чата. Токен вебхука
в конфиг НЕ писать: сервер сам читает его из env/.env.

### §Черновик сообщения в рабочий чат

Когда по выгрузке нужно ответить в чате — черновик пишется **простым текстом для
терминала**: без markdown-заголовков и таблиц, по-русски, с абсолютными путями
к файлам. Тот же формат используется для отчёта о дельте (п. 6 выше). Правила:
от первого лица, числа только из источников, обязательный финальный блок «что осталось»
с адресатом по каждому пункту.

## §Полезные ручные вызовы

Когда нужен один факт, без полной выгрузки:

```bash
WH="$BITRIX_WEBHOOK_URL"
curl -s -X POST "${WH%/}/tasks.task.get.json" -H 'Content-Type: application/json' \
  -d '{"taskId":123456,"select":["ID","TITLE","STATUS","STAGE_ID","GROUP_ID","CHAT_ID","RESPONSIBLE_ID","CREATED_BY","DEADLINE"]}'
curl -s -X POST "${WH%/}/im.dialog.messages.get.json" -H 'Content-Type: application/json' \
  -d '{"DIALOG_ID":"chat111111","LIMIT":50,"LAST_ID":25000000}'   # LAST_ID — листать в прошлое
curl -s -X POST "${WH%/}/im.recent.get.json" -H 'Content-Type: application/json' -d '{"LIMIT":50,"SKIP_OPENLINES":"Y"}'
curl -s -X POST "${WH%/}/im.counters.get.json" -H 'Content-Type: application/json' -d '{}'
curl -s -X POST "${WH%/}/im.dialog.get.json" -H 'Content-Type: application/json' -d '{"DIALOG_ID":"chat111111"}'
curl -s -X POST "${WH%/}/im.dialog.users.list.json" -H 'Content-Type: application/json' -d '{"DIALOG_ID":"chat111111"}'
curl -s -X POST "${WH%/}/im.search.chat.list.json" -H 'Content-Type: application/json' -d '{"FIND":"договор"}'
curl -s -X POST "${WH%/}/user.get.json" -H 'Content-Type: application/json' -d '{"FILTER":{"ID":[1,2]}}'
curl -s -X POST "${WH%/}/sonet_group.get.json" -H 'Content-Type: application/json' -d '{"FILTER":{"%NAME":"Проект"}}'
```

Другие полезные методы: `tasks.task.list` (фильтр по `GROUP_ID`), `task.stages.get`
(`entityId=<groupId>` — стадии канбана), `task.stages.movetask`, `im.v2.File.download`
(`dialogId` + `fileId` из `params.FILE_ID` → одноразовый `downloadUrl`),
`im.chat.user.list` (`CHAT_ID=` → id участников).

## Файлы

- `references/task-chat-export.md` — дословные транскрипты ошибок, формы ответов и
  разобранный пример выгрузки.
- `references/task-context-original.md` — исходное тело скилла `bitrix24-task-context`
  (сохранено дословно, расширенный набор граблей).
- `references/actualization-and-delta-report.md` — детальный рецепт отчёта о дельте
  и шаблон черновика сообщения.
- `scripts/fetch_bitrix_task_context.py` — основной скрипт: выгрузка задачи + её
  чатов, а также режимы `--chats` / `--unread` / `--find` / `--info`.
- `scripts/task_delta.py` — помощник дельты (baseline в `.delta_state.json`).
- `scripts/delta_since.py` — новые сообщения чата после указанного TS.
- `references/skill-chat-file-export.puml` — PlantUML-схема скилла и потока выгрузки вложений.
- `scripts/fetch_bitrix_task_context.rest-api-legacy.py` — прежняя, более простая
  версия того же скрипта (только задача + чаты, без режимов чатов). Оставлена для
  сравнения; новая версия superset-ит её.

## Pitfalls

- Don't print the webhook token; mask it in any dumped URL/error string.
- Messages authored by `author_id == 0` are system events (stage changed, call
  started/ended, deadline cleared) — render them as «система», don't quote as speech.
- `requests.post(..., json={})` on `im.counters.get` / `im.recent.get` is fine, but
  calling a script that never invokes its own `.env` loader silently dies with
  `KeyError: BITRIX_WEBHOOK_URL` — always assert the var is set before the first call.
- `im.chat.get {"CHAT_ID": N}` returns `null` under a webhook — expected, don't debug it;
  `im.dialog.messages.get` carries chat_id + users + files anyway.
- Chat attachments are linked by `params.FILE_ID`; names/sizes live in the response's `files[]`.
  For server-side download call `im.v2.File.download` with `dialogId` + `fileId`, then GET its
  one-time signed `downloadUrl`. Do not log that URL. `files[].urlDownload` is for an authorized
  Bitrix24 context; `disk.attachedObject.get` expects an attachment-record id, not a chat `FILE_ID`.
- `tasks.task.get` with `select` containing `UF_*` is fine, but some portals drop the
  whole card if any field is unknown — when the card comes back empty, retry with an
  explicit short `select` before concluding "no access".
- Timestamps arrive in the portal's timezone (offset embedded in the ISO string) — keep
  the offset in the export rather than localizing silently. Comparing a local "exported
  at" time with a portal timestamp without conversion silently hides new messages.
- `im.dialog.messages.get` response shape is not fixed: `users` arrives either as a dict
  `{id: {...}}` or as a **list** `[{id, name}, ...]` — normalize before `dict.update`.
  The `date` field is an ISO **string**, not a datetime.
- A bare number as a dialog id is ambiguous (a task id and a chat id can look alike);
  always prefix with `chat` when a dialog is meant.
