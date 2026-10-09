# LexRadar MVP v0.2 — Evidence Collector

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
