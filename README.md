# bitrix24-rest-api — скилл для Bitrix24 через REST-вебхук

Готовый набор для выгрузки данных из Bitrix24 (облако и коробка) через входящий
вебхук: карточка задачи, вся переписка вокруг неё, скачивание вложений из чатов,
список чатов и непрочитанное, дельты между выгрузками. Оформлен как skill для AI-агентов (Hermes, Claude Code,
opencode и любых, кто умеет читать `SKILL.md`), но скрипты полностью
самостоятельны — их можно гонять руками из терминала.

## Что внутри

```
SKILL.md                                        инструкция для агента/человека
scripts/fetch_bitrix_task_context.py            главный скрипт: задача + чаты + режимы чатов
scripts/task_delta.py                           дельта между выгрузками (baseline в .delta_state.json)
scripts/delta_since.py                          новые сообщения чата после заданного TS
scripts/fetch_bitrix_task_context.rest-api-legacy.py   упрощённая версия (только задача + чаты)
references/task-chat-export.md                  формы ответов API и дословные тексты ошибок
references/task-context-original.md             расширенный набор граблей и рецептов
references/actualization-and-delta-report.md    шаблон отчёта о дельте и черновика сообщения
references/skill-chat-file-export.puml           PlantUML-схема скилла и выгрузки вложений
```

## Быстрый старт

Нужен Python 3.9+ и `requests`.

```bash
python3 -m pip install requests          # или: python3 -m venv venv && . venv/bin/activate

# вебхук: Битрикс24 → Рабочее пространство → Разработчикам → Другое → Входящий вебхук
# права: task, im, user (для скачивания файлов чата достаточно im)
echo 'BITRIX_WEBHOOK_URL=https://<portal>/rest/<userId>/<token>/' > ~/.bitrix.env
chmod 600 ~/.bitrix.env
```

Проверка:

```bash
python3 scripts/fetch_bitrix_task_context.py --chats 5
```

## Использование

```bash
# задача: карточка + чат задачи + чаты, на которые ссылается описание
python3 scripts/fetch_bitrix_task_context.py 123456 --out-dir ./bitrix --limit 1000

# по ссылке на задачу
python3 scripts/fetch_bitrix_task_context.py --url "https://<portal>/company/personal/user/<uid>/tasks/task/view/123456/"

# задача + чаты + скачивание всех вложений из выгруженных сообщений
python3 scripts/fetch_bitrix_task_context.py 123456 --download-files --out-dir ./bitrix --limit 1000

# только конкретные чаты
python3 scripts/fetch_bitrix_task_context.py --dialog chat222222 --dialog chat333333 --out-dir ./bitrix

# чаты и непрочитанное
python3 scripts/fetch_bitrix_task_context.py --chats 40     # последние чаты со счётчиками
python3 scripts/fetch_bitrix_task_context.py --unread       # только непрочитанные + im.counters
python3 scripts/fetch_bitrix_task_context.py --find "договор"
python3 scripts/fetch_bitrix_task_context.py --info chat222222
```

Что появляется в `<out-dir>`:

```
task_<id>.json             сырьё: карточка, пользователи, сообщения, метаданные чатов
task_<id>.md               карточка + описание (чистый текст и raw bbCode) + список чатов
task_<id>_chat<NNN>.md     читаемая переписка: [дата] Автор (id) → текст, локальные ссылки на вложения
files/<dialog>/<fileId>_<имя>  скачанные вложения (при --download-files)
files/<dialog>/manifest.json   статусы, пути, размеры, sha256 и ошибки вложений
chats_recent.{md,json}     последние чаты: счётчик, тип, участники, последнее сообщение
chats_unread.{md,json}     непрочитанные + сводка im.counters.get
chats_find_<query>.json    поиск чатов по названию
<dialog>_info.json         карточка чата: участники, кто до чего прочитал
```

## Главная идея

В современном Битриксе обсуждение задачи живёт **в чате задачи**, а не в поле
комментариев. `tasks.task.get` отдаёт `CHAT_ID` — это и есть фид задачи; плюс
чаты, ссылки на которые лежат в описании (`IM_DIALOG=chatNNNNNN`). Метод
`task.commentitem.getlist` на облачных порталах часто отдаёт `WRONG_ARGUMENTS`
даже с корректным целым `TASKID` — это норма, а не баг скрипта.

Подробности, грабли (пагинация `LAST_ID`, bbCode, системные сообщения,
таймзоны, `fast_bitrix24` vs raw REST) — в `SKILL.md` и `references/`.

## Безопасность

Вебхук = полный доступ к порталу от имени создавшего его пользователя.
Не коммитьте `.env`, не печатайте токен в логи — скрипт маскирует его
(`redact()`), но при ручном `curl` проверяйте вывод. Одноразовые `downloadUrl`
для файлов также не записываются в логи, Markdown и manifest. Скилл рассчитан на
read-only сценарии: методов записи в Bitrix24 в нём нет.

## Лицензия

MIT — делайте что хотите, без гарантий.
