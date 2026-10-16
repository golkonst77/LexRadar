# Задание №52 — транспорт и безопасная публикация Single Auditor

Дата: 16.10.2026. Ветка: `feat/compact-ai-audit-mvp`.
Прочитаны отчёты №50–51, документация №50, прежние изменения транспорта,
тесты и потребители diagnostics. Состав №50 сохранён, посторонние изменения
MVP/compact pipeline/коммерческих документов не включаются.

## Исправление

Сохранены прежние полезные изменения provider.py: diagnostics с безопасными
категориями, учёт фазы/байтов/статуса, сохранение ссылки на socket до EOF,
обработка HTTPException, ограничения размера и времени. Проверка ненулевого
остатка Content-Length вынесена после цикла: закрытие HTTPResponse больше
не позволяет обойти проверку полноты. Короткий или пустой ответ с объявленной
большей длиной вызывает retryable ProviderError с kind=incomplete_read.
Обрыв chunked внутри данных, CRLF или до нулевого chunk корректно отклоняется.

Существовавшие семь транспортных тестов уже проходили на незакоммиченном
исправлении; дефект №51 воспроизводился на HEAD без него. В №52 добавлены
ещё 15 проверок: короткий/пустой Content-Length, реальные особенности read1,
разные обрывы/ошибки chunked, read timeout/URLError/OSError/HTTPException,
deadline после части тела, пустые полные ответы и закрытие HTTPError.
При расширении параметризации исправлено ожидание числа байтов: считаются
байты тела без chunk framing. Финальные прогоны успешны.

Контракт diagnostics согласован с существующими тестами и локальным compact
потребителем: четыре поля phase/kind/http_status/received_bytes, старые коды
и retryable сохранены, по умолчанию пустой словарь. Нет текстов исключений,
запросов, ключей, заголовков или ответов. Исключения не проглатываются:
ProviderError сохраняет причину через exception chaining; исходный traceback
не считается безопасным для публикации. HTTPError возвращает только статус
и пустое тело, ресурс закрывается.

Исследованы реальные исходники http.client в Python 3.13.5 из существующего
venv. read1 не выбрасывает IncompleteRead для короткого Content-Length,
оставляя положительный length после закрытия; это отдельно проверено тестом.
Chunked читает framing несколькими операциями и выбрасывает IncompleteRead
при отсутствии данных или следующего размера chunk. Известные ограничения:
EOF после нулевого chunk допускается без пустой строки trailers; ответ без
framing заканчивается по EOF; socket timeout и проверка между read1 не дают
жёсткого wall-clock deadline для медленного framing. Строгий HTTP validator
и произвольные socket wrappers не заявляются. Эти границы внесены в docs №50.

## Проверки

Без установки зависимостей, `/tmp/lexradar-029-venv` (Python 3.13.5):

- Транспорт + Single Auditor + локальные compact paid consumers:
  **61 passed in 1.70s** (22 + 22 + 17).
- Изолированная регрессия публикуемого состава, локальный clone
  `/tmp/lexradar-052-kpeu7awt/repo`, поверх HEAD только allowlist:
  **232 passed in 10.18s**. Запущены test_provider_transport,
  test_single_auditor_050, test_offline_mvp_048, test_auditors, test_mvp,
  test_decision, test_gateway и test_contracts. 210 прежних + 22 транспорта;
  два посторонних незакоммиченных теста MVP не включены.
- Для финальных прогонов sitecustomize запрещал DNS, socket.connect/connect_ex,
  включая дочерние Python-процессы. HTTP tests используют локальный HTTPResponse
  и fake opener, API-адаптеры — fake transports и синтетические credentials.
- Ruff check и format check семи Python-файлов, pip check, diff --check успешны.

Полный suite и другие версии Python не проверялись. Реальные API, платные
запросы, отправки клиентам, merge, deploy и изменение main не выполнялись.
Runtime skill прочитан; environment_status tool и network-policy snapshot
в данной среде недоступны. Секретные файлы и значения среды не читались.

## Состав публикации и безопасность

Явный allowlist из 12 файлов:

- src/lexradar/single_auditor.py
- src/lexradar/auditors/prompts/single-a-v1.txt
- src/lexradar/auditors/models.py
- src/lexradar/auditors/orchestrator.py
- src/lexradar/offline_mvp.py
- src/lexradar/auditors/provider.py
- tests/test_single_auditor_050.py
- tests/test_provider_transport.py
- docs/single-auditor-050.md
- docs/codex-reports/codex-050-report.md
- docs/codex-reports/codex-051-report.md
- docs/codex-reports/codex-052-report.md

Перед commit проверяются точное совпадение staged paths с allowlist,
staged diff, отсутствие whitespace ошибок, соответствие протестированным
исходникам и эвристический поиск секретов в полном staged содержимом.
Runs, досье, логи, approval, финансовые журналы и чужие изменения исключены.
Снимок SHA256 остальных tracked/untracked файлов хранится вне репозитория;
сохранность проверяется перед commit. Итоговый SHA и фактический статус push
сообщаются в ответе после выполнения Git, чтобы не создавать самоизменяемый
отчёт с собственным SHA. Отчёты №50–51 сохранены как история предыдущих проверок.
