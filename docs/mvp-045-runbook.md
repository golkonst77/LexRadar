# MVP №45: запуск без платных API

Из корня репозитория, Python 3.12+:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

Для PDF нужен FreeMono: в Debian/Ubuntu пакет `fonts-freefont-ttf`, файл
`/usr/share/fonts/truetype/freefont/FreeMono.ttf`. Можно передать `--font PATH`
с совместимым моноширинным TrueType-шрифтом (Windows BMP cmap format 4,
кириллица). Chromium для повторного анализа и PDF не нужен.

## Воспроизводимый запуск по сохранённым материалам

Каждый output должен быть новым. В этой среде вместо `.venv/bin/lexradar`
доступен `/tmp/lexradar-029-venv/bin/lexradar`.

```bash
.venv/bin/lexradar mvp https://medicpro-kaluga.ru/ \
  --dossier runs-036/medicpro-live-001/dossier --sector medical --pdf \
  --as-of 2026-10-15T13:30:00+00:00 --output runs-045/medicpro-new

.venv/bin/lexradar mvp https://kaluga.fomin-clinic.ru/ \
  --dossier runs-036/fomin-live-003/dossier --sector medical --pdf \
  --as-of 2026-10-15T13:30:00+00:00 --output runs-045/fomin-new
```

Дата `--as-of` — дата анализа, не свежего сбора и не подтверждения актуальности норм.
Исходные досье №36 локальные, не входят в Git; для другой машины перенесите каталог
целиком с `collection.json`, `artifacts/`, `crawl/`. Перед анализом проверяются
зарегистрированные хеши и привязка домена. Symlink запрещены. Новое досье копируется
в output; исходное не меняется. При отказе смотрите `run.json` и журналы.

Откройте локально `audit.html`, `audit.pdf` и `proposal.html`. Первый — отчёт
с инвентарём доказательств, цитатами, 13 этапами и юридическими вопросами;
второй — его текстовый PDF; третий — отдельный проект коммерческого предложения.
`audit.txt` содержит тот же текст, что PDF. Все полные факты и ограничения —
в `analysis/primary.local.json`; координаты цитат — в `trace-map.local.json`.
`sha256.json` фиксирует файлы результата. PDF рассчитан на текст и не содержит
скриншотов, активных ссылок или внешних ресурсов.

## Новый ограниченный публичный сбор

```bash
.venv/bin/lexradar mvp https://medicpro-kaluga.ru/ --static-only \
  --sector medical --pdf --max-pages 8 --output runs-045/medicpro-fresh
```

Повторите для второго домена с другим output. Нужны доступные публичные DNS и
прямой egress; robots, SSRF, pacing и лимиты сохраняются. При недоступности robots
сбор блокируется, частичные журналы остаются. Offline-подмена автоматически
не выполняется. `--fixture` применим только к синтетическому demo-домену.

AI всегда синтетический, деньги не расходуются, ключи не читаются, ledger/approval
не создаются. Итог HOLD/not_confirmed; выводов о нарушениях и автоматического допуска
к отправке нет. До клиентского использования специалисту нужно проверить текущие
формы, процессы, действующие нормы и применимость, затем согласовать состав,
срок и цену работ. Коммерческий проект не утверждает необходимость каждой услуги.

## Проверки

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

Для полного набора нужны localhost-сокеты и установленный Chromium
(`.venv/bin/python -m playwright install chromium`). Проверка без модулей,
которым нужны эти возможности:

```bash
.venv/bin/pytest -q --ignore=tests/test_collector.py \
  --ignore=tests/test_crawl_policy.py --ignore=tests/test_quality.py
```

Это отдельная ограниченная проверка, а не замена полному набору.

Для переносимого синтетического примера без досье №36:

```bash
.venv/bin/lexradar mvp https://demo.lexradar.example/ \
  --fixture examples/mvp-site --static-only --pdf --output runs-045/demo-new
```

Это проверка интеграции на учебных материалах; её нельзя выдавать за аудит реального сайта.
