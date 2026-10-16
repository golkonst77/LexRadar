# Чистая контрольная точка — 2026-10-16

Эта ветка имеет независимую историю из одного начального коммита. Исходная
история намеренно не включена, поскольку содержит клиентские материалы.
Производственный src и системные промпты сохранены побайтно.

Установка: Python 3.12+, `python -m venv .venv`, активировать окружение,
`python -m pip install -e '.[dev]'`. Проверка интерфейса: `lexradar --help`.
Для браузерного сбора отдельно требуется Chromium (`python -m playwright install chromium`).
Ключи задаются извне; в этой ветке их нет. Запуск `--help` не вызывает AI.
Профиль Single A: openai/gpt-5-mini, single-a-v3-cookie, без повторов.

Шаблон КП v2.4 исключён: исходник содержит конкретного клиентского адресата
и реальные контактные реквизиты. Оригинал не редактировался и сохранён
в исходной локальной резервной ветке. Рендеринг утверждённого КП без него
недоступен. Клиентские runs, старые отчёты, scripts и examples вне проверенного
списка исключены. Часть ссылок README и сценариев CI требует этих файлов.
Тест персонализированного КП исключён из-за клиентской привязки.

Известны 10 падений из задания №93; исправления не выполнялись:
- test_diagnostics_060.py: test_safe_diagnostics_and_retry[open/read],
  test_timeout_profile_and_transport[45/120], test_mvp_120_and_budget_retry (5).
- test_historical_openrouter_061.py:
  test_single_a_matches_historical_request_and_response_reader[length/chunked/eof] (3).
- test_transport_061.py:
  test_single_deepseek_requests_non_streaming_and_rejects_sse (1).
- test_mvp_batch_085.py: test_ten_organizations (1).
Причины: новые модель/лимиты/запрет retries и обязательный cookie review;
офлайн DEMO пока не соответствует новой схеме. Полный pytest/CI не заявляется
работоспособным: отсутствуют приватные фикстуры, scripts, examples и шаблон.
Нет lock-файла; версии зависимостей заданы диапазонами. Сеть и AI для
создания этой контрольной точки не использовались, кроме GitHub Git.
