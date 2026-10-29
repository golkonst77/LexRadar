# Codex №37 — screening и безопасный синтетический пилот

Дата: 2026-10-15. Изучены отчёты №31–36, актуальные preflight,
compact prepare/selection/quotes, profile/provider, budget и pipeline.
Ветка `feat/compact-ai-audit-mvp`, существующий Draft PR #28.
Платных запросов, клиентской передачи, human approval, merge и deploy нет.

## Подтверждённое ложное срабатывание

Старый regex `birth_date` считал датами `31.02.2020`, `29/02/2023`,
`00.12.2000`, `12.13.2000`. Это воспроизводимые синтетические контрпримеры,
а не классификация клиентских материалов. Теперь каждое совпадение этой
категории проходит календарную проверку `datetime.date`. Невозможная дата
не даёт этой категории; следующая настоящая дата в той же строке блокирует.
Все допустимые даты 1900–2099 остаются блокирующими вне зависимости от контекста.

Добавлены 15 регрессий: високосный год, короткая дата, несколько совпадений,
percent encoding, NFKC/fullwidth, zero-width, сохранение блокировок email,
телефона/идентификатора, медицинского контекста и ФИО. Отсутствие screening
не позволяет пройти authorize без approval. Тексты тестов синтетические.
Нормализация, остальные regex, проверка всех dictionary values, включая
неиспользуемые, exact SHA, повторная проверка исходников/цитат, ledger и HOLD
сохранены. Исключений для публичных телефонов, медицинских слов, ФИО и
ИНН/ОГРН не добавлено: происхождение само по себе не доказывает безопасность.

## Синтетический пакет и dry-run

Повторно подготовлено существующее `examples/compact-first-pilot/dossier`:
`.example`, одна страница, пустая форма, три учебных PDF, синтетический пиксель.
Реального сбора и заполненных полей нет. Ограничения fixture №32 сохранены:
не проверены реальные маршруты документов, видимость формы и screenshots.

Выполнено:

```bash
/tmp/lexradar-029-venv/bin/python scripts/dry_run_037.py
```

Скрипт запрещает socket.connect/create_connection и OpenRouterTransport.post,
проверяет integrity, все цитаты, повторяемость пакета, профиль и read-only
budget preview. Затем выполняет полный openrouter dry-run без approval и
с `approval_template=False`. Нет даже нового неподписанного approval-файла.
Повторный запуск не перезаписывает результаты; для него нужны новые пути.

Артефакты локально: `/tmp/lexradar-037-final/dry-run/` — packet.json,
primary.local.json, trace-map.local.json, messages.local.json (точные system/user),
preflight.json, compact-preview.json, compact-analysis.json, local-checks.json.
Старый review HTML не заменяет новые сообщения: system prompt менялся в №36.
Сам user packet остался побайтно прежним: **29105 bytes**, SHA-256
`7b15886086ccf838a14a9a974ebee722f732fa7629478e14289a745392292acd`.
Screening: `[]`; integrity/quotes/determinism/profile/budget preview — passed.

Итог **dry_run_blocked**, единственный blocker:
`Human approval of exact packet SHA-256 required`.
Это ожидаемая защита, а не dry_run_ready. `approval=null`, `usage=null`,
`request_sent=false`, `budget_charge_usd=0`, ledger и lock не созданы.
HOLD / not_confirmed / client_release_allowed=false.

Резерв **$0.009756687** = `(31493 × 0.259 + 2000 × 0.80) / 1000000`.
31493 — UTF-8 bytes обоих сообщений + 1024 framing; это консервативный bound,
не фактическая токенизация. Лимит сайта $0.05, output 2000, cache discount нет.
Старый резерв №34 ниже из-за прежнего system prompt. Существующий бюджет
реального аккаунта не подтверждён: `OPENROUTER_API_KEY` в окружении отсутствует,
авторизованных account GET и генераций не было. Изолированный пустой ledger
проверяет расчёт и лимит, а не баланс аккаунта или историю иных запусков.

## Модель и настройки

Публичный неавторизованный GET
[OpenRouter endpoints](https://openrouter.ai/api/v1/models/deepseek/deepseek-v3.2/endpoints)
15 октября подтвердил `deepseek/deepseek-v3.2`, `siliconflow/fp8`, status=0,
context 163840, max completion 147456, input $0.259/M, output $0.80/M,
параметры reasoning/response_format/structured_outputs/max_tokens.
Минимальный снимок: `examples/compact-first-pilot/openrouter-metadata-037.json`.
Это заявленная доступность в metadata; успешная генерация не проверялась.
Маршрут и цены не изменены; после публичной перепроверки срок профиля продлён
до **2026-10-16 12:00 UTC**, затем fail-closed.

Код оставляет only=[siliconflow/fp8], allow_fallbacks=false,
require_parameters=true, max_price, JSON object, reasoning.enabled=false,
one auditor, retries=0. Проверены официальные
[reasoning controls](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)
и [provider routing](https://openrouter.ai/docs/guides/routing/provider-selection).
Exclude лишь скрывает reasoning; реальное соблюдение настроек этим dry-run
не измеряется. Цена/параметры иных endpoints не используются для смены маршрута.

## Точная команда будущего платного пилота — НЕ выполнена

Сейчас запуск запрещён заданием. Перед отдельным разрешённым платным шагом
нужны просмотр новых messages/packet, действительный отдельно оформленный
approval exact SHA, безопасно переданный ключ, проверенный баланс, актуальные
metadata/профиль и повторный dry-run с тем же постоянным ledger. Approval в №37
не создаётся. `work/first-pilot/site-ledger.json` должен хранить всю историю
clinic.example; существующий ledger нельзя заменять новым для обхода расходов.
Каталог output должен быть новым. В текущем контейнере `work/` недоступен для
записи; это дополнительный блокер приведённой команды до исправления окружения.

```bash
PYTHONPATH=src /tmp/lexradar-029-venv/bin/python -c \
  'from lexradar.auditors.compact_pipeline import main; main()' \
  examples/compact-first-pilot/dossier \
  --output work/first-pilot/paid-037-001 \
  --config examples/compact-openrouter-config.json \
  --as-of 2026-10-10T00:00:00+00:00 --mode openrouter \
  --allow-external-transfer --approval work/first-pilot/approval.json \
  --ledger work/first-pilot/site-ledger.json
```

Для предшествующего dry-run к этой команде добавить `--dry-run` и заменить
output на новый каталог. Не повторять платную команду при timeout/unknown/
pending/overrun. После единственного разрешённого запроса сверить usage,
model/route, SHA, проверенные цитаты, ledger и Activity OpenRouter. Ошибка
валидации не отменяет возможное списание. Реальное качество AI не оценено.

## Проверки и блокеры

Целевые compact/preflight/paid/first-pilot/selection:
**61 passed за 36.36 s**, `/tmp/lexradar-037-targeted.log`.
Новые календарные регрессии отдельно: **15 passed**.
`ruff check .` — passed; `ruff format --check .` — 159 files already formatted;
`git diff --check` — passed.

Полный набор запущен с `PLAYWRIGHT_BROWSERS_PATH=/tmp/lexradar-029-browsers`:
**986 passed, 32 failed, 92 errors за 120.33 s**,
`/tmp/lexradar-037-pytest.log`. Полная регрессия НЕ зелёная:
92 setup errors collector/crawl-policy — TCP socket запрещён, PermissionError;
31 failure quality — TargetClosedError запуска Chromium и каскад отсутствующих
досье/отчётов; 1 failure batch orphan-worker — subprocess TimeoutExpired
после 2 секунд. Последний failure не объявляется доказанным последствием
sandbox; требует отдельного воспроизведения в обычном окружении. Защиты
окружения не отключались, unrelated batch/browser код не изменялся.

Платный пилот остаётся BLOCKED: запрет отправки этим заданием, отсутствие
human approval/ключа/подтверждённого баланса, непроверенный реальный ответ,
ограничения work и негарантированная полная регрессия. Истечение профиля или
изменение публичных metadata требуют новой проверки до оплаты.

## Публикация

В публикацию включены только семь явно выбранных текстовых файлов: preflight,
profile, 15 синтетических регрессий, dry-run script, минимальные публичные
metadata, инструкция и этот отчёт. Клиентские досье, локальные outputs,
ledger, approval, API-ключи и raw данные сайтов не включены. Изменения просмотрены;
синтетические контакты остаются только в тестах. Исторические отчёты сохранены.

Обычные git add/commit/push текущего worktree недоступны: git index находится
в read-only `/home/golkonst/src/LexRadar/.git/worktrees/LexRadar-compact`,
а git push --dry-run не разрешает DNS github.com. GitHub connector отклонил
`github_create_tree`: «MCP tool call requires approval, but approval policy is
never». Это автоматический отказ approval review; approval не запрашивался,
remote commit/обновление ветки и PR не выполнены. Дальнейшие внешние записи
не предпринимались. Публикация остаётся блокером.

Проверенные изменения сохранены отдельным локальным commit в
`/tmp/lexradar-037-repository` на ветке `feat/compact-ai-audit-mvp`, от исходного
HEAD `4fac91a828cf386cc403c3fb6830606adb81998b`. Bundle:
`/tmp/lexradar-037.bundle`. Это локальный переносимый результат, не push.
Основной worktree сохраняет изменения; его read-only HEAD/index не менялись.
Merge/deploy и изменение main не выполнялись.
