# Codex №34 — подготовка SiliconFlow FP8

Дата: 2026-10-14. Текущий worktree, ветка `feat/compact-ai-audit-mvp`.
Подготовка завершена; **платный запуск BLOCKED** до человеческого просмотра,
approval и отдельного разрешения отправки. Human approval не создавался.
Платных запросов, отправки досье, изменений main, merge, deploy, push не было.
Расход задания: **$0**.

## Публичная проверка

Выполнены только публичные GET через web, без ключа и досье.
[OpenRouter endpoints](https://openrouter.ai/api/v1/models/deepseek/deepseek-v3.2/endpoints)
заявляют модель `deepseek/deepseek-v3.2`, SiliconFlow, tag `siliconflow/fp8`,
quantization fp8, observed status=0, context_length=163840,
max_completion_tokens=147456. Цены за миллион: input **$0.259**, output **$0.80**,
cache read **$0.14**; per-token 0.000000259 / 0.0000008 / 0.00000014.
В supported_parameters есть response_format, reasoning, max_tokens и
structured_outputs. Снимок выбранных актуальных полей:
`examples/compact-first-pilot/openrouter-metadata-034.json`.
Это ручная запись полей текущего ответа, не полный raw response.

[Официальный Chat SDK reference](https://openrouter.ai/docs/client-sdks/python/api-reference/chat)
описывает response_format={type:json_object}.
[Reasoning docs](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)
описывают единый reasoning и enabled=false для отключения; exclude=true лишь
скрывает reasoning и не отменяет оплату. Metadata заявляют поддержку reasoning,
но успешное выполнение JSON mode и enabled=false на данном endpoint реальным
ответом **не проверялось**, поскольку запрос запрещён заданием.
Observed status=0 также не доказывает успешность будущего запроса.
Профиль сохраняет короткий срок до **2026-10-15 12:00 UTC**; затем fail-closed.

## Изменения и безопасность

- compact_profile.py и compact-openrouter-config.json: новые цены и endpoint limit.
  Бюджет конфигурации $0.05 на сайт; профиль дополнительно отклоняет увеличение
  лимита выше $0.05. Более низкий бюджет остаётся допустимым.
- provider.py использует PROVIDER из профиля вместо второго hardcoded маршрута:
  only=[siliconflow/fp8], allow_fallbacks=false, require_parameters=true.
  reasoning.enabled=false, JSON mode, retries=0, один auditor и max_tokens=2000.
- test_compact_paid.py проверяет точный новый маршрут, обе цены, JSON mode,
  require_parameters и output limit; отклоняет старые цены, retries>0,
  увеличение бюджета, лишний auditor и превышение endpoint limit.
  Существующие synthetic approvals остаются только во временных каталогах тестов;
  это не human approval пилота.
- Обновлены инструкция, generator и статический HTML с новым маршрутом/резервом.
  Досье, packet.json, primary/trace-map, старые отчёты и результаты №32/33 сохранены.
- Добавлены воспроизводимый dry_run_034.py, его результаты dry-run-034/ и metadata.

Screening, целостность, проверка цитат, точный SHA approval, изоляция ledger,
блокировки pending/unknown/overrun и исчерпанного бюджета, повторная авторизация
перед отправкой, защита ключа/размера ответа и HOLD не отключались.
Fallback на другой provider или модель не допускается; retries=0.

## Полный dry-run без отправки

Команда из корня (выполнена):

```bash
/tmp/lexradar-029-venv/bin/python examples/compact-first-pilot/dry_run_034.py
```

Скрипт запрещает socket.connect/create_connection и OpenRouterTransport.post.
Он проверяет saved fixture №32, артефакты и цитаты, точные байты пакета,
профиль и read-only SiteBudget.preview без обхода approval gate, затем вызывает
полный analyze_compact(mode=openrouter, dry_run=True) без approval.
Результаты сохранены в `examples/compact-first-pilot/dry-run-034/`.
Повторный запуск скрипта намеренно откажется перезаписывать существующий каталог;
для повторения задайте новый output и isolated ledger в локальной копии скрипта.

Результат **dry_run_blocked**; единственный blocker:
`Human approval of exact packet SHA-256 required`.
Независимые локальные проверки прошли, но это не dry_run_ready и не разрешение
отправки. approval=null, usage=null, request_sent=false, budget_charge_usd=0.
Ledger `/tmp/lexradar-034-pilot-ledger.json` и его lock не созданы.
Screening sensitive_categories=[], packet_bytes=29105.
SHA-256: `7b15886086ccf838a14a9a974ebee722f732fa7629478e14289a745392292acd`.
Резерв **$0.009720427** = (31353 × 0.259 + 2000 × 0.80) / 1000000.
31353 — консервативный bound UTF-8 system/user + 1024 overhead;
cache discount не используется. Это оценка, не фактические токены или списание.
Итог HOLD / not_confirmed / client_release_allowed=false.

## Проверки

```bash
/tmp/lexradar-029-venv/bin/python -m pytest -q \
  tests/test_compact.py tests/test_compact_pipeline.py \
  tests/test_compact_paid.py tests/test_first_pilot.py
/tmp/lexradar-029-venv/bin/python -m ruff check .
/tmp/lexradar-029-venv/bin/python -m ruff format --check .
git diff --check
/tmp/lexradar-029-venv/bin/python examples/compact-first-pilot/build_review.py
```

Результат: **39 passed in 23.76s**; лог `/tmp/lexradar-034-compact.log`.
Ruff check: **All checks passed**. Ruff format --check: **146 files already formatted**.
`git diff --check` прошёл. HTML пересобран, SHA и резерв совпадают с dry-run.
Полный pytest в №34 не запускался: задание требует 39 целевых тестов.
Последний полный suite №33 не зелёный (961 passed, 31 failed, 92 errors),
с ограничениями TCP sockets и Chromium sandbox; это исторический результат,
не повторная проверка №34. Полная регрессия требует разрешённого окружения.

## Точные команды следующего шага

Сейчас допустим только локальный просмотр и повторный dry-run. Откройте
`examples/compact-first-pilot/review/index.html`, проверьте оба сообщения,
оригиналы и SHA:

```bash
sha256sum examples/compact-first-pilot/review/packet.json
```

Ничего не подписывать автоматически. После самостоятельного личного просмотра
и отдельно разрешённого оформления approval используйте его существующий файл
`work/first-pilot/approval.json`. Шаблон examples/review/approval.json неподписан
и не подходит. Команда dry-run не создаёт approval:

```bash
mkdir -p work/first-pilot
/tmp/lexradar-029-venv/bin/python -c \
  'from lexradar.auditors.compact_pipeline import main; main()' \
  examples/compact-first-pilot/dossier \
  --output work/first-pilot/dry-034-next-001 \
  --config examples/compact-openrouter-config.json \
  --as-of 2026-10-10T00:00:00+00:00 --mode openrouter \
  --allow-external-transfer --approval work/first-pilot/approval.json \
  --ledger work/first-pilot/site-ledger.json --dry-run
```

Без действительного approval ожидается BLOCKED. Если файл существует и прошёл
проверку, ожидается dry_run_ready, тот же SHA и резерв; это ещё не платный запуск.
Проверьте публичные metadata повторно перед оплатой. При изменении цен/параметров,
истечении срока или недоступности закреплённого маршрута остановитесь;
не включайте fallback и не повышайте бюджет.

Только после отдельного разрешения платной отправки, выполнения остальных gates
и передачи OPENROUTER_API_KEY через безопасное окружение (не файл/историю),
следующая команда предназначена для **одного** будущего запроса и в №34 НЕ выполнена:

```bash
/tmp/lexradar-029-venv/bin/python -c \
  'from lexradar.auditors.compact_pipeline import main; main()' \
  examples/compact-first-pilot/dossier \
  --output work/first-pilot/paid-034-next-001 \
  --config examples/compact-openrouter-config.json \
  --as-of 2026-10-10T00:00:00+00:00 --mode openrouter \
  --allow-external-transfer --approval work/first-pilot/approval.json \
  --ledger work/first-pilot/site-ledger.json
```

Используйте тот же durable ledger для всех попыток на сайт; существующую историю
не обнулять. При timeout/unknown/pending/overrun не повторять запрос автоматически.
Сверьте usage.cost_usd, budget_charge_usd и ledger с OpenRouter Activity по request ID.
Реальная сверка списания сейчас невозможна: платной генерации не было.

Блокировки: отсутствует human approval; текущим заданием запрещена отправка;
поведение endpoint не проверено реальным ответом; полная регрессия №33 остаётся
неподтверждённой. Дополнительные возможные блокировки: expiry профиля,
изменённый SHA/артефакт, screening, неподходящий ledger или недостаточный бюджет.
Все изменения сохранены только в текущем worktree.
