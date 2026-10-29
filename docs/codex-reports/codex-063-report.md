# Задание №63 — CSV MVP для pre-production

Дата: 2026-10-16. Ветка `feat/compact-ai-audit-mvp`.
**PARTIAL для pre-production, READY для полностью офлайн-демонстрации.**
Практический CSV-конвейер реализован и проверен локально. Доступность текущего
OpenRouter/DeepSeek и сбор реальных сайтов не проверялись: это запрещено заданием.

## Что реализовано

Добавлена отдельная команда `python -m lexradar.mvp_batch`, переиспользующая collector,
load_packet, общий orchestrator, Single A профиль/адаптер, commercial и проверку цитат.
Существующие `lexradar mvp`, `batch-screen`, Single A, offline A/B не заменены.
Новая команда не использует старый mvp-экспорт, способный выпускать внутренние
черновики при HOLD, и не вызывает fake-генератор КП из Single A.

1. CSV: UTF-8 BOM/plain, запятая/точка с запятой, quoted values, пустые company/email,
   дополнительные колонки, канонизация URL, устранение дублей, изоляция неверного URL
   и количества полей. Неисправимый синтаксис CSV останавливает ввод до обработки.
2. Состояние: `manifest.json`, `results.csv`, `report.md`, отдельные каталоги строк
   и retry-попыток. Результаты записываются после каждой строки. SUCCESS/ERROR/SKIPPED
   отделены от коммерческих GO/HOLD; сохраняются stages и диагностические причины.
3. `prepare`: существующий ограниченный static collector → досье → проверка SHA и
   воспроизводимости текста → evidence packet → HOLD для ручного approval.
   Требуется отдельный `--allow-site-collection`. В №63 сеть не использовалась;
   ветка tested с локальной подменой collector, не реальным сайтом.
4. `offline`: локальное досье + явно записанный ответ → схема/цитаты/Single A →
   GO/HOLD → детерминированный HTML только при GO. Всё помечается DEMO/offline.
5. `paid`: только один сайт (`--max-sites 1`), `--allow-paid-single`,
   `--allow-external-transfer`, approval точного пакета. Записанный response не читается.
   Нет автоматического массового платного цикла. Следующие строки SKIPPED/site_limit.
6. HTML: компания, домен и проверенные точные цитаты конкретного сайта; HTML-экранирование
   всех входных данных; подтверждённый текст отделён от потенциального правового риска.
   Непроверенные ссылки на нормы, интерпретации AI, штрафы и гарантии не переносятся.
   Первичный аудит бесплатный; платная услуга ИП Голубева — юридические документы и
   подробная методичка вебмастеру; исправления сайта только по отдельному согласованию.
7. Возобновление offline/prepare с тем же входом/опциями, проверкой SHA сохранённых
   файлов и идентичности строк; завершённые ERROR тоже не повторяются автоматически.
   Прерванные pending получают новый каталог; прежние артефакты сохраняются.
   Платный resume намеренно запрещён, поскольку после обрыва расход может быть неизвестен.
8. Полностью офлайн-демонстрация одной командой: создаёт 4 синтетических сайта и
   дубль. Итог: SUCCESS 3, ERROR 1, SKIPPED 1; внутри SUCCESS — GO 2, HOLD 1;
   ровно два индивидуальных HTML. Нулевой API/сетевой расход.
9. Linux flock, отказ от перезаписи output, ограничения snapshot, отказ от symlink,
   source/output overlap и подмены путей resume. Данные дополнительных колонок —
   в manifest, необходимые dossier/response пути доступны в results.csv.

## Сохранённые механизмы безопасности

- Default MODEL по-прежнему **deepseek/deepseek-chat**, `single_auditor=True`, B отключён.
- DeepSeekAdapter и штатный non-streaming OpenRouter transport переиспользуются.
  Профиль: $0.50 на запуск, 4000 output tokens, input 200000 bytes,
  response 500000 bytes, timeout 45 s, retry 1, price caps $1/$2, no fallback.
- authorize вызывается до создания реального адаптера; orchestrator повторно
  проверяет разрешение перед платными попытками. Старое approval не разрешает
  другой SHA и не распространяется на остальные CSV-строки.
- MVP_NO_SCREENING только явным флагом paid; не отключает SHA, approval и бюджет.
- Offline использует только RecordedA; paid не подменяет ошибочный ответ синтетическим.
- `automatic_send_allowed=false`, `client_release_allowed=false`. Функций рассылки нет.
- `cost_usd=unknown`, если usage хотя бы одной платной попытки неизвестен;
  budget_charge_usd сохраняет отдельный консервативный учёт, а не выдаётся за списание.

## Исследование существующего состояния

Проверены ветка и dirty status, CLI dispatcher, batch-screen, mvp, Single A/live,
схемы finding/report, evidence/integrity, preflight approval, бюджет и orchestrator,
существующие синтетические тесты и материалы отчётов 47–61. Выводы прежней
инвентаризации учтены: старый mvp синтетический, A/B и fake proposal уже существуют;
они не дублировались в новом конвейере.

Локально прочитаны только выбранные метаданные исторического
`/tmp/lexradar-scz-single-003/analysis.json` и `requests.jsonl`:
completed=true, A success, request_sent=true, `deepseek/deepseek-chat`,
47.58943733399792 s, 29868 input / 1625 output tokens, $0.0093596607, prompt a-v3.
Текущий prompt single-a-v1 отличается. Это подтверждение сохранённого исторического
успеха, не точное воспроизведение его запроса и не гарантия нынешней доступности.
Полная побайтовая идентичность исторической конфигурации не утверждается —
ограничения доказательств подробно сохранены в отчёте 61.
Timeout после HTTP 200 и 440 байт не объявляется устранённым или объяснённым.

## Добавленные файлы

- `src/lexradar/mvp_batch.py` — CSV-конвейер, three modes, qualification, HTML,
  manifests, результаты и безопасное возобновление.
- `src/lexradar/mvp_demo.py` — воспроизводимые синтетические fixtures и one-command demo.
- `tests/test_mvp_batch_063.py` — 27 новых офлайн-сценариев.
- `docs/mvp-batch-runbook.md` — инструкция оператора и команды отдельных разрешённых этапов.
- `docs/codex-reports/codex-063-report.md` — этот отчёт.

Существовавшие modified/untracked файлы и посторонние артефакты не редактировались
и не удалялись. main не менялся. Git add/commit/push/merge/deploy не выполнялись.
Демонстрационные результаты находятся только в новых каталогах `/tmp`.

## Проверки

Новые 27 тестов: BOM/delimiters, дополнительные/пустые поля, дубли, неверные строки
и заголовки; полный demo, персонализация и HTML escaping, отсутствие HTML при HOLD
и ERROR, лимит, возобновление без повтора, прерванная попытка, изменённый CSV,
подмена SHA/manifest paths, symlink, отдельное разрешение на collection, prepare
с локальным collector, paid flags, неверный approval до ключа/транспорта, запрет
paid resume, отсутствие fallback на recorded response в paid, только A,
профиль/модель/бюджет и запрет сети/транспорта в offline.

Расширенная команда регрессии (внешние сокеты/DNS блокирует созданный
`/tmp/lexradar-063-guard/sitecustomize.py`, окружение реального ключа исключено):

```bash
env -u OPENROUTER_API_KEY PYTHONPATH=/tmp/lexradar-063-guard:src:tests \
 /tmp/lexradar-029-venv/bin/python -m pytest -q \
 tests/test_mvp_batch_063.py tests/test_historical_openrouter_061.py \
 tests/test_transport_061.py tests/test_provider_transport.py \
 tests/test_diagnostics_060.py tests/test_single_auditor_050.py \
 tests/test_mvp_no_screening_055.py tests/test_auditors.py \
 tests/test_batch.py tests/test_offline_mvp_048.py
```

Проверяет также существующие approval/SHA/budget guards, историческую совместимость
провайдера, частичные HTTP-ответы, diagnostics, запрет экспорта неподтверждённых цитат.
**Финальная расширенная регрессия: 233 passed in 30.92s.**
Отдельный целевой запуск: 27 passed in 0.78s (входит в эти 233).
Лог: `/tmp/lexradar-063-tests.log`. Все проверки офлайн.

```bash
/tmp/lexradar-029-venv/bin/ruff check src/lexradar/mvp_batch.py \
 src/lexradar/mvp_demo.py tests/test_mvp_batch_063.py
/tmp/lexradar-029-venv/bin/ruff format --check src/lexradar/mvp_batch.py \
 src/lexradar/mvp_demo.py tests/test_mvp_batch_063.py
git diff --check
```

Ruff check/format и diff check прошли. Полный Ruff всего dirty tree не запускался:
чужие незавершённые изменения не исправлялись. Полный suite проекта также не заявляется.

## Точные команды и готовые результаты

Один запуск, новый каталог:

```bash
env -u OPENROUTER_API_KEY PYTHONPATH=src /tmp/lexradar-029-venv/bin/python \
 -m lexradar.mvp_demo --output /tmp/lexradar-063-operator-demo
```

Уже проверенный пример: `/tmp/lexradar-063-final-demo/DEMO-results/results.csv`,
рядом manifest/report и HTML `sites/row-000002/proposal.html`,
`sites/row-000003/proposal.html`. SUCCESS 3, ERROR 1, SKIPPED 1. Resume выполнен
успешно, без повторной генерации и расходов:

```bash
env -u OPENROUTER_API_KEY PYTHONPATH=src /tmp/lexradar-029-venv/bin/python \
 -m lexradar.mvp_batch /tmp/lexradar-063-final-demo/DEMO-inputs/input.csv \
 --output /tmp/lexradar-063-final-demo/DEMO-results --max-sites 5 --resume
```

Сбор после отдельного разрешения на сайты:

```bash
PYTHONPATH=src /tmp/lexradar-029-venv/bin/python -m lexradar.mvp_batch leads.csv \
 --output runs/operator-prepared-001 --mode prepare --max-sites 5 --allow-site-collection
```

Один реальный аудит после разрешения передачи/бюджета и ручного approval:

```bash
PYTHONPATH=src /tmp/lexradar-029-venv/bin/python -m lexradar.mvp_batch runs/operator-one.csv \
 --output runs/operator-paid-001 --mode paid --max-sites 1 \
 --allow-paid-single --allow-external-transfer \
 --packet-approval runs/operator-one-approval.json
```

Две последние команды не выполнялись. Форматы CSV/approval, расходы, диагностика
и возобновление подробно описаны в `docs/mvp-batch-runbook.md`.

## Ограничения и оставшиеся блокеры

- Не подтверждён реальный end-to-end успех нынешнего провайдера: запросы запрещены.
  Исторический успех не снимает риск read timeout и возможного списания при ошибке.
- Полнота/актуальность реального сбора, JS-форм и применимых норм требует отдельной
  проверки. Static evidence и grounded quote сами по себе не доказывают нарушение.
- HTML намеренно консервативный: проверенные цитаты и контекст компании, без
  автоматически придуманных правовых выводов. Юрист должен проверить его перед выпуском.
- Paid batch ограничен одним сайтом, нет автоповтора paid resume и общего массового
  лимита на несколько независимых запусков. Каждый запуск требует отдельного решения.
- Offline в этом маршруте всегда DEMO даже при replay реального сохранённого текста;
  не использовать его как отчёт о новом реальном AI-аудите.
- Linux file lock; последовательный pipeline; нет CRM, UI, рассылки или deploy.
  Нет внешнего supervision/deadline для всего процесса, кроме лимитов collector/provider.
- Resume требует прежний CSV и завершённые файлы; исходники pending могут обновиться,
  они заново проходят load_packet. ERROR повторяется только в новом output.
- CSV без dossier достаточен для prepare; offline требует dossier+response, paid —
  проверенное dossier. Сбор и inference разделены человеческим approval.
- Пакетные ошибки представлены в сводке даже при успешном exit code CLI.

После возвращения оператору нужны: свой CSV, разрешение на ограниченный сбор либо
готовые досье; проверка точного исходящего пакета; новое approval и разрешение
единичного расхода до $0.50; затем оценка реального результата и ручная проверка КП.
Ключ не читался, платных/сетевых запросов, внешних передач и отправки писем не было.

Финальная повторная демонстрация после всех правок: `/tmp/lexradar-063-verified-demo/DEMO-results/results.csv`; DEMO=true, GO 2, HOLD 2 (включая ERROR), unevaluated 1, HTML 2, automatic_send_allowed=false.
