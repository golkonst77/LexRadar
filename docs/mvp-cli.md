# Сквозной MVP: URL → досье → compact AI → HTML

Команда `lexradar mvp` всегда использует существующий `CompactFakeTransport`.
Она не имеет платного режима, не читает ключ OpenRouter, не создаёт approval,
не отправляет досье и не рассылает предложения. HTML — предварительный черновик,
production outcome остаётся HOLD. Это проверка интеграции, не качества настоящей LLM.

## Установка в обычном серверном окружении

Из корня этого worktree, Python ≥3.12:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m playwright install chromium
```

Браузерному режиму нужны системные зависимости Chromium и разрешённый запуск
браузера. Не отключайте sandbox или сетевые защиты. Если браузер недоступен,
явно выбирайте `--static-only`: сбор сохранит HTML, текст, PDF и ограничения,
но не скриншоты или поведение динамического интерфейса. Формы первичный анализ
извлекает из сохранённого HTML, без отправки.

## Полностью offline, одна команда

```bash
.venv/bin/lexradar mvp https://demo.lexradar.example/ \
  --fixture examples/mvp-site --static-only \
  --output work/mvp-offline-001 --as-of 2026-10-14T12:00:00+00:00
```

Это синтетический сайт: две HTML-страницы и текстовый PDF. URL и локальная
fixture связаны строго; fixture нельзя выдать за реально собранный публичный сайт.
Не нужны localhost, DNS, Chromium или API-ключ. Robots, pacing и лимиты сохранены.

## Публичный сайт в обычном окружении

```bash
.venv/bin/lexradar mvp https://example.com/ \
  --output work/mvp-public-001 --max-pages 3
```

Или та же команда с `--static-only` при отсутствии браузера. Сеть используется
только для ограниченного публичного GET-сбора; AI остаётся локальным fake.
Транспорт требует разрешённых публичных DNS и прямого egress. Если окружение
требует прокси, текущий DNS-pinned collector остановится; обхода прокси нет.
Нельзя подменять публичные адреса localhost или отключать robots/SSRF/бюджет.
Поддерживается ограниченный обход одного origin; PDF скачиваются и извлекаются,
прочие документные форматы пока не обрабатываются. JS и отправка форм запрещены.

## Результаты

- `proposal.html`: коммерческий черновик с конкретными фактами, источниками,
  потенциальными вопросами и услугой ИП Голубева.
- `dossier/collection.json`, `dossier/artifacts/*`, `dossier/crawl/*`: оригиналы,
  текст, PDF, robots, журнал и сводка обхода.
- `analysis/primary.local.json`, `trace-map.local.json`, `packet.json`,
  `preflight.json`, `compact-preview.json`, `compact-analysis.json`: первичные
  факты, точные цитаты, компактный пакет, screening и результат fake AI.
- `config.json`, `run.json`, `sha256.json`: конфигурация, итог и контрольные суммы.

Каталог output должен быть новым: повторное использование завершится ошибкой,
старые результаты не перезаписываются. При ошибке возврат CLI — 1, `run.json`
и доступные частичные материалы сохраняются; HTML при отсутствии доказательств
не создаётся. При слишком большом compact packet запуск останавливается,
ничего не отбрасывает молча и не отправляет.

Открывайте HTML как локальный файл. Для ручной серверной проверки интеграции:

```bash
.venv/bin/python -m pytest -q tests/test_mvp.py tests/test_compact_pipeline.py
.venv/bin/python -m pytest -q tests/test_collector.py tests/test_crawl_policy.py
```

Вторая команда требует доступного localhost и Chromium. В ограниченном
окружении ошибки этих зависимостей не означают успешную проверку сборщика.
