# LexRadar MVP v0.3 — Independent AI Auditors

## Граница реального юридического решения

Добавлен [учёт достоверности текста и чтения HTML/PDF](docs/evidence-reading-integrity.md):
повторное извлечение, постраничные ограничения и локальный недоверенный журнал просмотра.

Команда `lexradar decide` проверяет локальную целостность и привязку рассмотрения,
но в текущем MVP всегда возвращает **HOLD / not_confirmed**. Доверенное происхождение
рассмотрения и нормативных источников пока удостоверить нельзя. Демонстрационный
Gateway v0.1, включая его GO, не предоставляет реального клиентского допуска.
См. [workflow и ограничения](docs/trusted-decision-boundary.md).

Технический фундамент юридико-технического аудита сайтов по законодательству РФ
о персональных данных. Обработка полностью локальная, вход и выход — JSON.

## Установка и запуск

Python 3.12+, из корня репозитория:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
pytest -q
ruff check .
ruff format --check .
lexradar examples/input.json --output work/report.json
```

Перед последней командой создайте `work/` (`mkdir -p work`). Без `--output` JSON
печатается в stdout. Невалидный JSON отклоняется Pydantic. Пример использует
зарезервированный домен `.example` и исключительно синтетические данные.

## Состав

- `src/lexradar/models.py` — типизированные контракты и проверка ссылок.
- `gateway.py` — чистая детерминированная функция решения.
- `report.py` — внутреннее досье и сдержанный черновик только для GO.
- `approval.py` — фиксация человеческого допуска и запрет отправки.
- `ports.py` — интерфейсы будущих Collector и независимых аудиторов.
- `cli.py` — офлайн обработка JSON.
- `tests/` — проверки правил, контрактов и запрета отправки.
- `.github/workflows/ci.yml` — pytest, Ruff и воспроизводимость примера.

[Архитектура и правила](docs/architecture.md) · [Ограничения](docs/limitations.md)

Отсутствие подтверждённых нарушений означает NURTURE с причиной
`no_confirmed_violation_no_outreach_basis`, без клиентского черновика.
Это не утверждение о полном соответствии сайта законодательству.

## Evidence Collector

`lexradar collect URL --output work/audit` собирает только технические факты.
[Установка, защита SSRF, лимиты и ограничения](docs/collector.md).
Для браузерных тестов установите Chromium: `python -m playwright install --with-deps chromium`.
Collector не подключён к Gateway, не формирует юридические выводы и не отправляет сообщения.

## Структурированный первичный аудит (PR #10)

`lexradar primary-audit work/audit --output work/primary` работает offline с сохранённым
досье. 13 рабочих направлений, матрицы организаций/форм, наблюдения, гипотезы,
нормативные кандидаты и задания на перепроверку разделены. Результат внутренний,
юридически неполный, HOLD/not_confirmed; клиентские материалы не создаются.
[Запуск, этапы, сверка v2.5 и примеры](docs/primary-audit-methodology.md).

## Independent AI Auditors

`lexradar analyze work/audit --output work/analysis` по умолчанию работает offline без LLM.
[Архитектура, настройка OpenRouter, бюджет и ограничения](docs/auditors.md).
Реальный запуск требует конфигурации, `OPENROUTER_API_KEY` в окружении и
`--mode openrouter --allow-external-transfer --packet-approval work/review/approval.json`.
Перед этим обязателен локальный `lexradar preflight`: проверка человеком точного пакета
по SHA-256 и блокировка при признаках чувствительных данных. Эвристики не гарантируют
отсутствие ПДн. A/B получают одинаковые доказательства
в отдельных запросах с разными промптами. Результаты не подключены к Gateway.

## Independent Legal Verifier

Локальные карточки нормативных оснований и редакций: [workflow PR #9](docs/trusted-legal-sources.md).
`lexradar legal-source prepare/check/review` проверяет сохранённый текст, цитаты и
заявленные периоды, сохраняя unverified/untrusted и production HOLD.

v0.4 сохраняет самостоятельный результат до чтения A/B, затем выполняет отдельное
сопоставление. `lexradar verify` и `lexradar verify-compare` работают offline по умолчанию.
[Архитектура, запуск, реестр норм, полнота и ограничения](docs/verifier.md).
Каждый внешний пакет требует своего preflight и человеческого утверждения SHA-256.
Поставляемый реестр содержит только неподтверждённые шаблоны; проверенных НПА нет.
Результат не подключается к GO и не отправляется клиентам.

Реестр трактует JSON как недоверенные заявления: официальный URL, локальный хеш
и human_official_review не дают current_confirmed. `lexradar attest-source` фиксирует
отдельное заявление независимого проверяющего, которое также остаётся unverified.
До реализации надёжной аутентификации конвейер не выдаёт verified_issue.

## v0.5: испытания качества

19 синтетических сценариев проходят Collector, независимые A/B и Stage I/II Verifier с
записанными локальными ответами; платных запросов нет. Эталоны, метрики и отчёты отделены от LLM.
Результат требует ручной проверки: намеренные FP, FN и расхождение A/B остаются видимыми.

```bash
lexradar benchmark --suite tests/benchmarks --output work/benchmark
lexradar quality-report work/benchmark --format html
```

[Методика, replay, экспертная разметка и controlled pilot](docs/quality-testing.md).
Пилот выключен по умолчанию и выполняет только отдельно разрешённый публичный сбор.
Ни испытания, ни экспертные метки не создают доверенную норму, автоматический GO или рассылку.

## Безопасный публичный Collector

Collector проверяет robots.txt для LexRadar, соблюдает запреты и Crawl-delay, выполняет
последовательные запросы с задержкой по умолчанию 2 с, общим лимитом попыток и срока обхода.
Недоступный или неоднозначный robots.txt блокирует обход; журнал находится в `crawl/` досье.

```bash
lexradar collect https://example.org/ --output work/public-pilot \
  --max-pages 10 --max-documents 10 --timeout 10 \
  --min-delay 2 --max-requests 64 --max-duration 300
```

[Политика, журнал, ограничения и требования к сетевой среде](docs/collector-pilot-safety.md).
Обязательный proxy по-прежнему не поддерживается pinned транспортом; обход ограничений запрещён.
