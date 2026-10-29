# Задание 35: работающий сквозной offline MVP

Дата: 14 октября 2026. Изменения только в worktree
`feat/compact-ai-audit-mvp`. Main, merge, deploy, push не выполнялись.
Платных API-запросов, внешней передачи досье, человеческого approval,
автоматической рассылки не было. Расход **$0**.

## Реализованный результат

Добавлена единая CLI-команда `lexradar mvp`. Переиспользованы существующие
collector runner, CrawlSession, robots, ArtifactStore, обработка PDF,
primary audit, compact prepare/preflight, budget, auditor/provider,
CompactFakeTransport, валидация цитат и HTML-шаблон commercial_preview.
Архитектура не заменена. Новый static adapter явно отключает браузерную
часть, фиксирует недоступность скриншотов и оставляет технический статус partial.
Статические формы извлекает существующий primary audit из оригинального HTML.

Команда MVP допускает только fake AI, без платного переключателя и ключей.
Добавлен параметр отключения шаблона approval в существующий preflight;
MVP не создаёт даже неподписанный approval.json. Обычные команды и защитные
ограничения сохраняют прежние defaults. Проверка цитат, screening, бюджет,
HOLD / not_confirmed / client_release_allowed=false сохраняются.

HTML разделяет подтверждённые технические факты и потенциальные вопросы,
даёт ссылки на страницы/PDF и локальные артефакты. Конкретные находки демо:
форма обратного звонка с полем телефона, в проверенном статическом фрагменте
0 checkbox и 0 ссылок на политику внутри формы; политика доступна отдельной
страницей; условия заказа доступны PDF. Эти факты не объявляются нарушениями.
AI помечен синтетическим; совпадение цитаты не выдаётся за юридическое заключение.
Продавец — ИП Голубев. Предлагается платная подготовка документов и подробной
инструкции вебмастеру; внедрение выполняет вебмастер заказчика. Цена и срок
согласуются после уточнения процессов. Штрафы и правовые выводы не выдуманы.

## Реально выполненная успешная команда

Из корня репозитория, используя существующий установленный venv:

```bash
/tmp/lexradar-029-venv/bin/lexradar mvp https://demo.lexradar.example/ \
  --fixture examples/mvp-site --static-only \
  --output examples/mvp-035/demo --as-of 2026-10-14T12:00:00+00:00
```

Exit **0**, stdout:
`success; AI simulated; USD 0; HTML: examples/mvp-035/demo/proposal.html`.
**2 страницы + 1 PDF, 3 доступных evidence**; robots received,
queue_exhausted; 4 локальных fixture-read попытки, **0 сетевых запросов**.
Compact analysis status=success, одна проверенная синтетическая observation;
usage.cost_usd=0, request_sent=false, approval=null, budget_charge_usd=0.
Это настоящий прогон всей интеграции по синтетическим входам, не реальный
юридический AI-анализ и не сбор живого сайта. PDF извлечён существующим worker.

Готовый пример: [proposal.html](../../examples/mvp-035/demo/proposal.html).
Артефакты: `examples/mvp-035/demo/`:

- `dossier/collection.json`, `dossier/artifacts/page-000*.html`, `.txt`,
  `document-0001.pdf`, `crawl/requests.jsonl`, robots и summary;
- `analysis/primary.local.json`, `trace-map.local.json`, `packet.json`,
  `preflight.json`, `compact-preview.json`, `compact-analysis.json`;
- `config.json`, `run.json`, `sha256.json` — все файлы прогона с SHA-256.

Fixture: `examples/mvp-site/`. Повторная команда требует нового output:
существующие артефакты не перезаписываются. Ранее выполненные результаты
`offline-success/` сохранены; первый неудачный `offline/` показывает ошибку
Content-Type robots в адаптере fixture, исправленную перед успешным прогоном.

## Реальный сбор и ограничения среды

Выполнено:

```bash
/tmp/lexradar-029-venv/bin/lexradar mvp https://example.com/ \
  --static-only --max-pages 1 --output examples/mvp-035/public-static \
  --as-of 2026-10-14T12:00:00+00:00
```

CLI exit **1**. Публичный сайт реально не собран: DNS resolution failed
на robots.txt; политика deny_all сохранена. 0 доступных evidence,
HTML не создан, AI не запускался. Досье, журнал и run.json сохранены
в `examples/mvp-035/public-static/`. Сеть не обходилась, web-tool не
подменял реальную работу collector.

Браузерная попытка той же demo fixture без `--static-only`:

```bash
/tmp/lexradar-029-venv/bin/lexradar mvp https://demo.lexradar.example/ \
  --fixture examples/mvp-site --output examples/mvp-035/browser-attempt \
  --as-of 2026-10-14T12:00:00+00:00
```

CLI exit **1**: Chromium executable отсутствует по пути Playwright cache.
Журнал stop=collector_error и ошибка сохранены в `browser-attempt/`.
Отдельная попытка создать TCP socket для localhost завершилась
`PermissionError: [Errno 1] Operation not permitted`:
`examples/mvp-035/localhost-check.json`. Sandbox не отключался.

Воспроизводимая установка и запуск для обычного сервера:
[docs/mvp-cli.md](../mvp-cli.md). После установки зависимостей и Chromium:

```bash
.venv/bin/lexradar mvp https://example.com/ \
  --output work/mvp-public-001 --max-pages 3
```

Это команда реального collector с локальным fake AI. Она подготовлена,
но успешный серверный результат в этой среде **не подтверждён**.

## Проверки

Выполненные команды и результаты записаны в `examples/mvp-035/checks.json`
(сводка, не raw лог).

```bash
/tmp/lexradar-029-venv/bin/python -m pytest -q \
  tests/test_mvp.py tests/test_compact.py tests/test_compact_pipeline.py \
  tests/test_compact_paid.py tests/test_first_pilot.py
```

**43 passed in 25.95s**. Новые 4 теста проверяют полный прогон с запретом
DNS/socket/OpenRouterTransport, наличие страниц/PDF/HTML и хешей,
отсутствие approval, неизменность HOLD, отказ перезаписывать output,
отказ маскировать fixture под публичный сайт, escaping ссылок и сохранение
run.json при ошибке collector. Существующие тесты paid gate используют лишь
синтетические временные fixtures; реальный approval не создавался.

```bash
/tmp/lexradar-029-venv/bin/python -m pytest -q \
  tests/test_static_text.py tests/test_static_mechanisms.py tests/test_primary.py \
  tests/test_crawl_policy.py tests/test_gateway.py
```

**252 passed, 51 errors in 3.71s**. Все 51 ошибки — setup локального
HTTP-сервера test_crawl_policy, запрещено создание TCP socket.
Полная регрессия не запускалась и не объявляется зелёной.
Ruff check: All checks passed; итоговый format --check: 152 files already formatted;
`git diff --check`: passed. Независимо сверены SHA-256 всех 18 файлов
финального demo-прогона; расхождений нет, MVP approval-файлов нет.

## Препродакшн к 16 октября: остающиеся препятствия

1. Проверить реальный сайт и browser collector на сервере с публичным DNS,
   разрешённым прямым egress, localhost и установленным Chromium; выполнить
   collector/crawl integration и полную регрессию. Прокси текущий pinned
   транспорт не поддерживает; нет обхода политики окружения.
2. AI сейчас fake: он демонстрирует интеграцию/проверку цитат, а не качество
   юридического анализа. Настоящий AI в задании запрещён. Качество модели и
   legal review не подтверждены; никакого разрешения отправки нет.
3. Обход ограничен одним origin, 5 страницами/3 PDF/20 запросами/90 секундами
   по defaults MVP. Другие форматы документов не извлекаются. Dynamic JS,
   визуальные согласия и фактическая передача форм не проверены. Большой
   dossier может не вместиться в 40 KB compact packet — fail-closed.
4. Предложение требует ручной проверки фактов и актуальных применимых норм,
   уточнения оператора/процессов и согласования цены/срока. Автоматическая
   доставка клиенту не реализована и не разрешена. HOLD остаётся в силе.

Работающий доступный сквозной сценарий выполнен и сохранён. Успешный сбор
живого сайта и готовность к препродакшну этим отчётом не утверждаются.
