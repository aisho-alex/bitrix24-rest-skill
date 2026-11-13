# Task-chat export — raw evidence and worked example

Session: a cloud Bitrix24 portal, webhook user `<uid>`, task taken from URL
`.../user/<uid>/tasks/task/view/123456/`. Real portal ids and people are replaced with
placeholders below; the shapes and error texts are verbatim.

## Verbatim failures (kept so future sessions don't re-diagnose them)

`fast_bitrix24` (python 3.12 venv):

```
get_all() doesn't support parameters 'start' or 'order': not self.st_params
    or set(self.st_params.keys()).isdisjoint({"START", "ORDER"}):
self.st_params was {'ORDER': {'POST_DATE': 'asc'}, 'TASKID': '123456'}
```
→ `bx.get_all("task.commentitem.getlist", params={"TASKID": id, "ORDER": {...}})` is structurally
impossible; drop ORDER and sort locally, or use raw REST.

```
{'order0000000000': {'error': 'ERROR_CORE', 'error_description':
 'TASKS_ERROR_EXCEPTION_#256; Param #0 (taskId) for method ctaskcommentitem::getlist()
  expected to be of type "integer", but given something else.; 256/TE/WRONG_ARGUMENTS'}}
```
→ raised both for `bx.call(..., {"TASKID": 123456, "ORDER": ...})` and for `{"TASKID": 123456}`.
Raw REST returned the identical error, i.e. this portal genuinely refuses the method — not a
client bug. The task feed is served by the task chat instead. Treat `commentitem` as
best-effort only: catch, note it in the output, move on.

```
ViolationError: call(): item_list can't be empty
```
→ `bx.call("im.recent.get", {})` and `bx.call("profile")`.

Also noisy but harmless: `UserWarning: It's better to use get_all() with methods that end with
('.list', '.getlist', ...)` whenever `bx.call` is used on such a method; suppress with
`warnings.catch_warnings()` if it clutters the script output.

## Response shapes seen

`tasks.task.get` (select incl. `CHAT_ID`) → flat camelCase card plus nested `group`, `creator`,
`responsible` objects that already carry `name` — no extra `user.get` needed for the card.
Key fields: `chatId: 111111`, `stageId: "4516"`, `groupId: "873"`, `createdBy: "1"`,
`responsibleId: "2"`, `accomplices`/`auditors` as id arrays, `descriptionInBbcode: "Y"`.
Note: `select` without `CHAT_ID` finds no chat at all — always include it.

`im.dialog.messages.get` (`DIALOG_ID=chat222222`, `LIMIT=3`) → single-key batch wrapper:

```json
{"order0000000000": {"chat_id": 222222,
  "messages": [{"id": 25085056, "chat_id": 222222, "author_id": 0,
                "date": "2026-09-16T12:17:55+04:00",
                "text": "[USER=44843 REPLACE]Иванов Иван[/USER] вышел из чата",
                "params": {"CODE": ["CHAT_LEAVE"], "NOTIFY": "N"},
                "replaces": [], "uuid": null}],
  "users": [{"id": 55684, "name": "Петрова Мария", "work_position": "Специалист "}],
  "files": []}}
```
- `author_id: 0` + `params.CODE` ∈ {CHAT_JOIN, CHAT_LEAVE} ⇒ service event (count separately).
- `params` is sometimes `[]` (list) instead of a dict — guard before `.get`.
- `replaces` carries live "-text" auto-links, e.g. `{type: DATE, text: "сегодня", value: ...}`.
- Newest first; `LIMIT` above 50 is not honoured — page with `LAST_ID = min(id)`.

`im.chat.get {"CHAT_ID": 222222}` → `null` (webhook lacks IM rights). Not fatal, don't chase it.

`user.get {"FILTER": {"ID": [...]}}` → one user dict per id, max 50 ids per call; `PERSONAL_MOBILE`
comes back partially masked (`+79****1234`), so don't build phone lists from it.

## Worked example: task 123456

A project task in group 873, постановщик «Пользователь 1» (id 1), ответственный
«Пользователь 2» (id 2), создана 16.09 11:18, дедлайн 21.09 18:00. Numbers below are from
a real export; only names/ids are placeholders.

Chats discovered and dumped (all three, full history):

| dialog | what | msgs | content msgs | period |
|---|---|---|---|---|
| `chat111111` | task chat (from `task.chatId`) | 140 | 140 | 16.09 12:18 → 18.09 12:38 |
| `chat222222` | "Основной чат обсуждения" (from description) | 202 | 176 | 24.08 15:01 → 16.09 12:17 |
| `chat333333` | "Техническая встреча" (from description) | 32 | 29 | 15.09 14:00 → 16.09 15:25 |

`task.commentitem.getlist` → 0 rows + WRONG_ARGUMENTS. Output written to
`<out-dir>/`: `task_123456.json` (624K), `task_123456.md`,
`task_123456_chat{111111,222222,333333}.md` (~124K each for the big two).

## URL anatomy

`https://<portal>/company/personal/user/<uid>/tasks/task/view/<taskId>/`
- `<uid>` = whose personal section is open (may equal the webhook owner, may not); irrelevant to access.
- `<taskId>` = the only thing the script needs.

## Access notes

- Read-only REST against a SaaS portal needs no confirmation.
- Mask the webhook token whenever dumping URLs or error text.
- Runs from any Python 3.9+ with `requests` installed.
