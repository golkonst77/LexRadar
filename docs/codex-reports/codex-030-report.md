# Codex №30 — compact AI pipeline

Дата: 2026-10-14. Ветка: `feat/compact-ai-audit-mvp`, продолжение Draft PR №28.
Повторная проверка и публикация выполняются по явному поручению пользователя.
Цель публикации — существующий Draft PR №28; новая ветка и новый PR не создаются.
Main, merge и deploy не затрагиваются. Платные API-запросы не выполняются.

## Реализовано

- Изучены compact-primary-v1, существующие orchestrator/provider/budget/validator,
  preflight и тесты. Legacy pipeline сохраняется; compact orchestration вынесена
  в `auditors/compact_pipeline.py`, команда `lexradar compact-analyze`.
- Короткий системный prompt A версии compact-A-v1 объясняет dictionary/keys/$,
  отделяет недоверенные материалы от инструкций и требует дословные цитаты.
  Компактная Pydantic JSON-схема включена в system message; provider использует
  существующий JSON-object response_format. Схема проверяется локально строго.
  Ответ: auditor A, observations с excerpt/quote/hypothesis/status, checks.
  Только potential/unverifiable; confirmed и дополнительные поля отклоняются.
- User message содержит неизменённый compact packet. Записываются SHA-256 packet
  и prompt, версия prompt, модель, usage, статус и признак симуляции.
- Новый провайдер не создан: используется OpenRouterProvider с прежними
  сериализацией, контролем модели/секретов/лимитов и разбором envelope.
  CompactFakeTransport имитирует envelope модели и цитату из sent excerpt.
  Offline не читает API key, использует фиктивный ключ и требует встроенный fake
  transport. Новая CLI предоставляет только offline режим.
- Отдельный compact_validator воспроизводит оригиналы через verify_quotes и
  проверяет каждую цитату через verify_quote в конкретном sent excerpt.
  Совпадение доказывает только вхождение текста. Юридическое нарушение и
  правильность интерпретации не подтверждаются. Отчёт всегда HOLD,
  legal_status=not_confirmed, client_release_allowed=false.
- Preflight сохраняет packet, screening, неподтверждённый approval template,
  local primary и trace map. Новый output обязателен, внутри source и через
  symlink он запрещён. Повторный запуск в тот же output отклоняется.
  Offline может завершить симуляцию при подозрениях, но внешняя передача остаётся
  запрещённой. Это не approval и не исключение из screening.
- Для оплачиваемого API-вызова analyze_compact требует явное разрешение передачи,
  постоянный ledger и существующий approve точного SHA-256. Перед расходом
  повторяет prepare/approve. Screening никогда не отключается. Автоповторов нет.
- SiteBudget хранит записи по hostname одного сайта. Обычный лимит 0.05 USD,
  абсолютный допустимый лимит 0.50 USD. Ранее сохранённый меньший лимит не
  повышается повторным запуском. Резерв использует существующий консервативный
  byte/token-price bound до передачи. flock защищает read/modify/write,
  temp-file + fsync + replace + fsync каталога обеспечивают долговечность.
  Пути канонизируются, повреждённые/отрицательные записи отклоняются.
- Pending сохраняется до отправки: crash блокирует следующий запуск. При ошибке
  транспорта/парсинга или неизвестной стоимости сохраняется полная оценка со
  статусом unknown. Unknown/pending/overrun блокируют дальнейшие расходы.
  Известная стоимость записывается без сокрытия превышений; превышение резерва
  делает overrun и прекращает успешное завершение. Абсолютный лимит ограничивает
  разрешённые резервы; неожиданный счёт провайдера физически отменить невозможно.

## Offline воспроизведение

```bash
lexradar compact-analyze examples/primary-audit/dossier \
  --config examples/compact-offline-config.json \
  --as-of 2026-10-14T00:00:00+00:00 \
  --output /tmp/lexradar-030-review
```

Синтетический config использует лимит 0.05, фиктивные model IDs и retries=0.
API key не требуется. Успешный результат: `success; HOLD; offline simulated
completion`. Кроме preflight-файлов создаётся `compact-analysis.json`.
CLI возвращает ненулевой код при неуспешной валидации.
Фактический финальный smoke запуск выполнен в `/tmp/lexradar-030-final-offline`; ответ провайдера
имитировался локально, расход 0. Клиентские досье не использовались; OpenRouter
запросов не было. Локальные выводы досье не включены в изменения репозитория.

## Проверки

Новые тесты проверяют полный offline success и CLI, неверную цитату, запрет
confirmed, ошибочный ответ через весь pipeline, отказ screening до транспорта,
повторный output, долговечность расхода между экземплярами, исчерпание бюджета,
неизвестную стоимость, crash/pending, overrun, абсолютный предел, повреждённый
ledger и конкурирующие резервы. Сокеты и реальный OpenRouterTransport в этих
тестах запрещены. Существующие compact tests покрывают original tampering,
цитату из другой выдержки, exact approval hash и sensitive blocking.

Первоначальный sandbox-прогон дал 939 passed, 31 failed, 92 errors из-за
запрета сокетов и запуска Chromium. При повторной проверке в текущей разрешённой
среде использованы существующие Python environment и Chromium задания №29:

```bash
PLAYWRIGHT_BROWSERS_PATH=/tmp/lexradar-029-browsers \
  /tmp/lexradar-029-venv/bin/python -m pytest -q
/tmp/lexradar-029-venv/bin/ruff check .
/tmp/lexradar-029-venv/bin/ruff format --check .
git diff --check
```

- Полный pytest: **1066 passed**, 195.16 s; failures/errors нет.
- Compact + auditors + single-auditor: **106 passed**, 8.79 s.
- Ruff по всему репозиторию: **All checks passed**.
- Ruff format: **137 files already formatted**.
- `git diff --check`: без ошибок.
- Повторный offline CLI smoke: `success; HOLD; offline simulated completion`,
  output `/tmp/lexradar-030-publication-offline`, без внешнего транспорта и расходов.
- Просмотрены все 8 изменённых/новых файлов. Поиск приватных ключей, токенов
  OpenRouter/OpenAI/GitHub/AWS, присвоений секретов, email и российских телефонов
  не дал совпадений. При ручном просмотре клиентские данные не обнаружены;
  config и tests синтетические, локальные досье/outputs/backups не публикуются.
- Перед публикацией HEAD совпадает с origin/feat/compact-ai-audit-mvp.
  PR №28 OPEN, Draft, head=feat/compact-ai-audit-mvp, base=main.

## Публикация

Результаты №30 публикуются одним коммитом в `feat/compact-ai-audit-mvp` и
существующим Draft PR: https://github.com/golkonst77/LexRadar/pull/28.
Заголовок и описание PR обновляются под итог №29–30. PR остаётся Draft.
Новый PR не создаётся; main, merge, deploy и платные API-запросы не выполняются.
Файлы не удаляются.

## Препятствия для первого платного пилота

1. Текущий conservative screening обнаруживает медицинскую лексику даже в
   синтетическом primary packet и блокирует отправку. Exact approval этого не
   обходит. Нужен разрешённый пакет, проходящий действующие проверки; смягчение
   screening в этом задании не делалось.
2. Нужны проверенные модель/тарифные caps, безопасно настроенный ключ, человеческий
   review packet и approval его конкретного SHA-256, явное разрешение передачи.
   Оплачиваемый успешный запрос не запускался и не проверялся реальным сервисом.
3. Оператор должен выбрать единый постоянный ledger для всех запусков сайта,
   сохранять и резервировать его; новый/удалённый ledger теряет историю.
   flock рассчитан на локальную Unix filesystem, не распределённый deployment.
   Несколько hostname в досье пока отклоняются для paid режима.
4. Unknown/pending/overrun требуют независимой сверки счёта. Автоматической
   reconciliation или команды сброса нет: неизвестный расход не списывается в ноль.
5. SiteBudget использует Unix `fcntl` и не поддерживает Windows; Windows CI
   локально не проверен. Нужны независимые проверки фактов и действующего права;
   HOLD не превращается в клиентский выпуск.
