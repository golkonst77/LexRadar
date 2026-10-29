# Задание 066 — интеграция сохранённого реального аудита и CSV MVP

Итог: **PARTIAL**, doktstom.ru — **HOLD**. Реальный результат DeepSeek признан
реальным и успешным технически; качество юридических выводов проверено отдельно.
Клиентское предложение не создавалось. Новых AI-вызовов и расходов — 0.

## Реальные материалы и результат

Изучены:

- `runs-064/doktstom-deepseek-chat-001/analysis.json`, `run.json`, `requests.jsonl`;
- `runs-054/doktstom-improved/dossier/collection.json`, сохранённые HTML/TXT;
- воспроизведённый evidence packet и его ограничения, формы, provenance и SHA.

Пакет: `42d33ebbf0e329b6ea76bb04bdcb9f844877a8f046c18610ce378ca8ab7f06f9`.
Manifest: `0e7f728a9f130d4f7e46b8c0e0ca30cefb18d71734b6bfb12468b26bb55d46b0`.
Они совпали с реальным анализом и разрешением в run.json. Журнал requests.jsonl
полностью совпал с logs анализа. Модель `deepseek/deepseek-chat`, только A,
completed=true, один запрос 2026-10-16, 37.693 секунды, timeout профиля 120 секунд.
9596 input + 1813 output = 11409 токенов; исторический расход **$0.0043350435**.
В исходном запуске был явно включён MVP_NO_SCREENING. Он не перенесён как новое
разрешение ни на этот, ни на какой-либо другой сайт.

Досье содержит одну доступную страницу `page-0001`, статус partial, HTML 153398
байт, TXT 27127 байт, без PDF и без доступного скриншота. Это локальный производный
импорт с исправлением токенов, а не новая загрузка сайта. Исходная дата заявлена
2026-10-13T15:08:32.556600Z, локальный импорт 2026-10-16T09:03:45.429942Z;
origin_authenticated=false, HTTP-статус отсутствует. Полнота извлечения текста
не означает полноту оригинала или сайта. JS, CSS-видимость, отправка формы,
внешняя запись через Medflex и действующие нормы не исследовались.

Название ООО «СТОМАТОЛОГИЧЕСКИЙ ЦЕНТР ДОКТОР СТОМ» взято из футера сохранённой
страницы. Это наблюдаемая подпись сайта, а не проверка юридического лица по реестру.
Новые сведения о реквизитах или контактах не придумывались.

### Оценка находок

| Находка | Проверка факта | Юридическая применимость | Итог |
|---|---|---|---|
| consent_form_issue | Точная цитата про нажатие «Отправить» найдена в TXT и HTML | Полный сценарий неизвестен. AI не обосновал перенос требований к письменному согласию на любую веб-форму; актуальность и основания обработки не проверены | PARTIAL, не пригодна для GO |
| privacy_policy_issue | Фраза «Сайт обеспечивает безопасность персональных данных от несанкционированного использования их третьими лицами.» присутствует | Общая фраза не доказывает отсутствия мер и не обосновывает обязанность публично раскрывать все технические меры; внутренних документов нет в выборке | PARTIAL, не пригодна для GO |

Полная первая цитата и точные evidence_id/source сохранены в evidence-review.json.
Дополнительно HTML на строках 1094–1096 показывает кнопку «Записаться», тогда как
пояснение говорит «Отправить»; AI неверно превратил упоминание в тексте в название
кнопки. Это подтверждённое текстовое расхождение, но самостоятельный юридический
дефект и его применимость не доказаны. В политике также есть иной текст согласия;
отсутствие отдельного полного согласия по ограниченной выборке не установлено.
Никакого категорического обвинения или размера штрафа не сформировано.

Обе находки уже имели fact_supported=false, status=unverifiable, evidence_quality=partial,
а ссылки на нормы — actuality_status=unverified. completed=true это не исправляет.
Действующий строгий фильтр также требует точного совпадения fact с привязанной
цитатой; парафразы этих находок его не проходят. Критерии ради GO не ослаблялись.

## Готовые артефакты

- [Реальный внутренний HTML](../../runs-066/doktstom-real/report.html).
- [Решение](../../runs-066/doktstom-real/decision.json),
  [проверка доказательств](../../runs-066/doktstom-real/evidence-review.json),
  [находки](../../runs-066/doktstom-real/findings.json),
  [происхождение и расход](../../runs-066/doktstom-real/provenance.json).
- [Финальный реальный results.csv](../../runs-066/accepted/real-batch/results.csv),
  manifest и исходный CSV рядом. Cost/budget нового чтения = 0, historical_cost отдельно.
- [Демонстрационный results.csv](../../runs-066/demo-accepted/DEMO-results/results.csv):
  SUCCESS 3 (GO 2, HOLD 1), ERROR 1, SKIPPED 1 (дубль); два явно DEMO HTML.
- `runs-066/checks/`: логи проверок; `html-render.json` объясняет блокировку Chromium.

`accepted/` — финальный воспроизводимый снимок. `real-batch/`, `verified/`, `final/`
сохраняют промежуточные результаты этого задания, не заменяют финальный снимок.
Повторные вызовы saved_audit проверяли локальные файлы, платного replay не было.

## Изменения

- `src/lexradar/mvp_batch.py`: режим saved для настоящего AnalysisReport, SHA/model
  recheck, раздельный исторический/новый расход, лимиты вызовов/бюджета, timeout 45/120,
  NEEDS_APPROVAL, безопасный paid resume, проверка устаревшего состояния после lock,
  независимый юридический допуск production GO, атомарный HTML, адаптивный/A4 CSS.
- `src/lexradar/batch_paid_guard.py`: консервативный общий бюджет и лимит попыток,
  durable `paid-attempt.json` перед внешней границей; неопределённый расход не освобождает резерв.
- `src/lexradar/batch_qualification.py`: независимый локальный разбор факта,
  применимости нормы и исправимости. Привязка к SHA пакета и нормализованного AnalysisReport.
  Подтверждение не может сделать пригодной находку, отвергнутую действующим фильтром.
- `src/lexradar/evidence_review.py`: VERIFIED/PARTIAL/UNSUPPORTED по каждой находке,
  точные цитаты, source, причины, JSON-решение, внутренний readable report для HOLD.
- `src/lexradar/saved_audit.py`: одна локальная команда проверки runs-064, сверка
  трёх исходных файлов и генерация batch + требуемых реальных артефактов.
- `tests/test_integration_066.py`: 18 новых офлайн-сценариев, включая реальный fixture,
  GO после локального юридического разбора, HOLD/ERROR/SKIPPED/дубль, повторный запуск,
  XSS в цитатах, лимиты, неизвестную стоимость, timeout 120 и повреждённый ledger.
- `tests/test_mvp_batch_063.py`: обновлены ожидания NEEDS_APPROVAL, безопасного
  перечитывания paid и HOLD при отсутствии независимой юридической проверки.
- `docs/mvp-batch-runbook.md`, `docs/examples/mvp-066-leads.csv`,
  `docs/examples/mvp-066-one-local.csv`, `docs/mvp-acceptance-066.md`, этот отчёт.
- Новые локальные артефакты в `runs-066/`. Прежние runs и чужие изменения не перезаписывались.

Исходные dirty изменения в mvp.py, single_auditor.py/live, orchestrator, models,
compact_pipeline, commercial_preview и соответствующих старых тестах/документах
не откатывались и не редактировались этим этапом. Ветка сохранена:
`feat/compact-ai-audit-mvp`. Commit/push/merge/deploy не выполнялись.

## Проверки и второй проход

Целевой набор: **45 passed**. Расширенный набор: **251 passed**.
Лог финального набора: `runs-066/checks/regression-accepted.log`.
Это расширенная регрессия затронутых компонентов, не заявление о полном suite проекта.

```bash
env -u OPENROUTER_API_KEY PYTHONPATH=src:tests /tmp/lexradar-029-venv/bin/python -m pytest -q tests/test_integration_066.py tests/test_mvp_batch_063.py tests/test_historical_openrouter_061.py tests/test_transport_061.py tests/test_provider_transport.py tests/test_diagnostics_060.py tests/test_single_auditor_050.py tests/test_mvp_no_screening_055.py tests/test_auditors.py tests/test_batch.py tests/test_offline_mvp_048.py
/tmp/lexradar-029-venv/bin/ruff check src/lexradar/mvp_batch.py src/lexradar/mvp_demo.py src/lexradar/evidence_review.py src/lexradar/batch_paid_guard.py src/lexradar/batch_qualification.py src/lexradar/saved_audit.py tests/test_integration_066.py tests/test_mvp_batch_063.py
/tmp/lexradar-029-venv/bin/ruff format --check src/lexradar/mvp_batch.py src/lexradar/mvp_demo.py src/lexradar/evidence_review.py src/lexradar/batch_paid_guard.py src/lexradar/batch_qualification.py src/lexradar/saved_audit.py tests/test_integration_066.py tests/test_mvp_batch_063.py
git diff --check
```

В тестах новых маршрутов удалён OPENROUTER_API_KEY и запрещены DNS/socket/реальный
transport. Платный путь проверяется только локальными stub; синтетические записи
остаются в pytest tmp и не выдаются за production-аудиты. Проверены модель,
единственный A, отсутствие Qwen, no-send, approval с другим SHA, нулевой call cap,
малый/NaN/бесконечный бюджет, запись резерва до вызова, отсутствие автоматического
повтора при timeout, safe resume и блокировка uncertain pending.

Второй проход исправил риск изменения состояния до lock, учёт неизвестной стоимости
при исключении после попытки, зависимость SHA юридического разбора от повторной
валидации, сохранение копии юридического разбора в каталоге сайта и атомарность HTML.
Проверены XSS через CSV и точную цитату, привязка предложений только к разрешённым
находкам, отсутствие proposals для всех не-GO. Офлайн resume real и DEMO выполнены.

Локальная визуализация Chromium не запустилась: sandbox_host_linux.cc,
shutdown Operation not permitted. Скриншоты и PDF не заявляются готовыми.
Новые зависимости и расширенные разрешения не запрашивались. CSS содержит
адаптивные правила, @page size:A4 и print; реальная визуальная приёмка остаётся PARTIAL.

## Ограничения и следующий этап

1. Новый live end-to-end запрещён заданием и не выполнен. Исторический успех runs-064
   подтверждён; сбор актуального сайта и новый аудит требуют отдельных разрешений.
2. Paid остаётся одно-сайтовым. Массовый paid с набором отдельных approvals BLOCKED;
   подготовка нескольких пакетов и чтение сохранённых результатов работают пакетно.
3. Независимое правовое исследование не автоматизировано. Без него production GO
   заблокирован даже при подходящей цитате. Локальный legal-review — заявление
   проверяющего, а не криптографическая подпись или автоматическая проверка нормы.
4. При uncertain paid pending нельзя обходить блокировку новым output: сперва
   сверка расходов и журналов, затем отдельное решение. Защита относится к одному
   output; глобального реестра расходов между произвольными каталогами нет.
5. Внутренний orchestrator консервативно пишет request_sent перед provider.complete;
   отклонённый wrapper повтор может фигурировать в его логе как возможная отправка.
   Фактическое число разрешённых транспортных попыток и внешний резерв отражены в
   ai_calls, paid-attempt.json и results.csv; неопределённый расход не считается нулевым.
6. Нужны визуальная приёмка A4/мобильного HTML и проверка внедрения документов.
   Автоматическая отправка отсутствует, выпуск клиенту по-прежнему не разрешён.

Первая безопасная команда после возвращения оператора (новый каталог):

```bash
env -u OPENROUTER_API_KEY PYTHONPATH=src /tmp/lexradar-029-venv/bin/python -m lexradar.saved_audit --output /tmp/lexradar-066-operator-review
```

Демонстрация одной командой:

```bash
env -u OPENROUTER_API_KEY PYTHONPATH=src /tmp/lexradar-029-venv/bin/python -m lexradar.mvp_demo --output /tmp/lexradar-066-operator-demo
```

Дальнейшие prepare/paid/saved-команды, форматы approvals и юридического разбора,
просмотр результатов и resume приведены в runbook. Они не являются разрешением
внешних обращений. До нового согласования используйте только локальную проверку.
