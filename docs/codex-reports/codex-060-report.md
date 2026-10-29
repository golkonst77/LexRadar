# Задание №60 — безопасная диагностика Single DeepSeek A

Дата: 2026-10-16. READY — локальное исправление и офлайн-проверки завершены.

## Изменения

Изучены отчёт №59, OpenRouterProvider/HTTP-транспорт, RequestLog, orchestrator,
Single Auditor и live entrypoint. В RequestLog добавлено необязательное типизированное
provider_diagnostics. Orchestrator сохраняет только phase=open/read,
kind=timeout/deadline/incomplete_read/http_protocol/network, целочисленный HTTP-статус
100–599 и неотрицательный целочисленный received_bytes. Каждое значение проверяется
отдельно; неизвестные поля, произвольные строки, неверные типы и bool вместо числа
отбрасываются. Диагностика сбрасывается перед каждой попыткой. Старые журналы без
нового поля читаются. Оба формата — analysis.json и requests.jsonl — сохраняют
одинаковую безопасную диагностику. Текст исключений, cause, заголовки, ключи,
содержимое запросов и ответов в новое поле не копируются.

profile(timeout_seconds=120), DeepSeekAdapter(timeout_seconds=120) и явный CLI-флаг
--timeout-seconds 120 разрешают увеличенное ожидание только для Single Auditor MVP.
Без флага остаётся 45 секунд. Допустимы только 45 и 120; остальные параметры
фиксированного профиля по-прежнему проверяются, включая MVP_NO_SCREENING gate.
Эффективный timeout сохраняется в run.json. Общий AnalysisConfig и HTTP-транспорт
не изменены, новая архитектура не вводилась.

## Измерение без сети

Настоящее тело JSON перехвачено локальной подменой transport.post на входе,
через существующие messages_for и DeepSeekAdapter.complete. Использовался только
синтетический ключ; реальный ключ и секретный файл не читались. Payload не выводился
и не сохранялся. socket.connect/connect_ex и getaddrinfo были заблокированы.

Пакет: runs-054/doktstom-improved/dossier.
SHA256 payload: 42d33ebbf0e329b6ea76bb04bdcb9f844877a8f046c18610ce378ca8ab7f06f9.

| Состав | UTF-8 bytes |
| --- | ---: |
| Пакет, user content | 34319 |
| System content, включая JSON-схему | 11133 |
| Сумма content обоих сообщений | 45452 |
| Полное сериализованное JSON-тело HTTP-запроса | 47216 |

Последнее значение включает поля модели, messages, JSON escaping и параметры
генерации/маршрутизации. HTTP-заголовки, TLS и сетевой framing в размер JSON-тела
не входят. Пакет не увеличен; max_input_bytes=200000 и max_response_bytes=500000
сохранены. Точное число токенов модели офлайн не утверждается.

## Бюджет и запреты

Лимит профиля остаётся 0.50 USD; модель deepseek/deepseek-v3.2; max_tokens=4000;
price caps prompt=1, completion=2 USD/миллион; fallback запрещён; retries=1.
Существующий Budget считает консервативную границу 45452+1024=46476 input tokens:
резерв 0.054476 USD на попытку, 0.108952 USD на две попытки, ниже 0.50 USD.
Неизвестный usage не освобождает резерв. Это локальный бюджетный расчёт,
не фактический расход и не проверка текущего баланса/тарифов провайдера.
Существующие проверки бюджета до вызова provider сохранены и прошли регрессию.
automatic_send_allowed=False проверен; live entrypoint не вызывает отправку клиентам.

Существующий runs-058/packet-approval.json успешно проверен через authorize с
mvp_no_screening=True против текущего пакета; approval не создавался и не менялся.
Обычный screening сохранён. Следующий запуск использует явный MVP_NO_SCREENING,
как первый запуск: он отключает только screening, остальные gates остаются.

## Проверки

```bash
env -u OPENROUTER_API_KEY PYTHONPATH=/tmp/lexradar-059-check:tests:src /tmp/lexradar-029-venv/bin/python -m pytest -q tests/test_diagnostics_060.py tests/test_single_auditor_050.py tests/test_provider_transport.py tests/test_mvp_no_screening_055.py tests/test_auditors.py
/tmp/lexradar-029-venv/bin/ruff check .
/tmp/lexradar-029-venv/bin/ruff format --check src/lexradar/auditors/models.py src/lexradar/auditors/orchestrator.py src/lexradar/single_auditor.py src/lexradar/single_auditor_live.py tests/test_diagnostics_060.py
```

Результат: 134 passed in 12.38s; Ruff — All checks passed; 5 files already formatted.
Новые минимальные регрессии: обе фазы timeout в обоих журналах, успешный повтор без
старой диагностики, чтение старого JSON, исключение секретных/невалидных полей,
45/120 на fake transport, сохранение бюджетного профиля, запрет изменения остальных
параметров, MVP gate при 120, две ошибочные попытки в пределах бюджета и запрет
автоматической отправки. Использован существующий сетевой блокиратор №59.

Посторонние изменения сохранены. src/lexradar/auditors/provider.py и исходные
артефакты не изменялись. Реальные API-запросы, commit, push и deploy не выполнялись.
Исправление сохраняет свидетельства следующей ошибки; устранение внешнего timeout
не гарантируется. Текущая доступность модели/валидность реального ключа не проверялись.

## Следующий реальный запуск — НЕ выполнен

Из корня репозитория. Новый каталог runs-060/doktstom-deepseek-a-002 пока отсутствует.
Команда читает ключ только в память дочернего процесса, без печати и передачи в argv:

```bash
PYTHONPATH=src /tmp/lexradar-029-venv/bin/python -c 'import os, runpy; from pathlib import Path; os.umask(0o077); os.environ["OPENROUTER_API_KEY"] = Path("/home/golkonst/.config/lexradar/openrouter.key").read_text().strip(); runpy.run_module("lexradar.single_auditor_live", run_name="__main__")' runs-054/doktstom-improved/dossier runs-060/doktstom-deepseek-a-002 --packet-approval runs-058/packet-approval.json --allow-external-transfer --mvp-no-screening --timeout-seconds 120
```
