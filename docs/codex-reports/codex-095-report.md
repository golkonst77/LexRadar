# LexRadar №95 — исправление известных падений тестов

Дата: 2026-10-20. Рабочий каталог проверен: `/home/golkonst/src/LexRadar-compact`.
Изучены `docs/codex-reports/codex-091-report.md`, `codex-092-report.md`
и `runs-093/report.md` (отдельного docs-отчёта №93 нет).
Создана и выбрана `fix/lexradar-095-tests` от
`backup/lexradar-preproduction-2026-10-16`, HEAD `b3a8cd5a51b8c0ee139e63b73962e0142cebc81a`.
Main и резервная ветка не изменены, коммиты не создавались.

## Причины и минимальные исправления

Исходный запуск четырёх целевых модулей: **10 failed, 9 passed**.

| Тест | Случаев | Классификация и исправление |
| --- | ---: | --- |
| test_safe_diagnostics_and_retry | 2 | Устаревшее ожидание повтора Single A. Проверяется единственный отказ даже при переданном retries=1; очистка diagnostics и чтение старых логов сохранены. Добавлены два случая обычного режима с успешным повтором. |
| test_timeout_profile_and_transport | 2 | Устарели default 45 секунд и недопустимость retries=0. Default теперь 120; 45/120 поддержаны, 60 отклоняется. Для проверки некорректной конфигурации используется retries=1. |
| test_mvp_120_and_budget_retry | 1 | Устаревшее ожидание двух запросов. Проверяются один вызов, ошибка транспорта, бюджет и запрет отправки. Провайдер синтетический. |
| test_single_a_matches_historical_request_and_response_reader | 3 | Исторический ответ DeepSeek сравнивался с новым default GPT-5 Mini. Для обоих реализаций явно выбран DeepSeek; retries=0. Побайтовое сравнение запроса и три способа HTTP framing сохранены; исторический snapshot не изменён. |
| test_single_deepseek_requests_non_streaming_and_rejects_sse | 1 | Для теста DeepSeek явно задана модель, сохранены лимит 4000, non-streaming, запрет fallback и отклонение SSE. |
| test_ten_organizations | 1 | Настоящая ошибка DEMO: генератор не выдавал обязательный cookie-раздел. В mvp_demo добавлен NOT_VERIFIED с причиной и ограничениями, без доказательств; cookie coverage отмечен insufficient_evidence. Тест проверяет этот результат для каждой организации, а также прежние 8 GO / 2 HOLD и изоляцию КП. |

Производственное изменение только в `src/lexradar/mvp_demo.py`.
Архитектура, cookie-валидация, модельный контроль и запрет повторов Single A сохранены.

## Проверки

Окружение: `/tmp/lexradar-029-venv/bin/python`, `PYTHONPATH=src`.
Доступное соседнее `.venv` не содержит pytest; зависимости не устанавливались.

Финальная команда:
```sh
PYTHONPATH=src /tmp/lexradar-029-venv/bin/python -m pytest tests/test_diagnostics_060.py tests/test_historical_openrouter_061.py tests/test_transport_061.py tests/test_mvp_batch_085.py tests/test_mvp_batch_063.py::test_demo_end_to_end tests/test_approved_proposal_084.py -q
```
**27 passed**: все исходные 10 падений устранены, дополнительные проверки
повторов, операторского DEMO и утверждённого КП проходят.
Ruff check пяти изменённых Python-файлов: **All checks passed**.
`git diff --check`: без ошибок.

Дополнительный запуск полных модулей test_mvp_batch_063, test_integration_066
и test_approved_proposal_084 вместе с целевыми: **68 passed, 3 failed**.
Вне согласованного списка остались:
- `test_mvp_batch_063.py::test_model_and_budget`: ожидает DeepSeek по умолчанию;
- `test_integration_066.py::test_real_csv_to_hold_and_resume`: исторический saved-analysis отклонён текущим saved mode;
- `test_integration_066.py::test_real_hash_and_second_auditor_rejected`: ожидает отклонения другой модели старым Single DeepSeek контрактом.
Эти сценарии не исправлялись; полный набор тестов не запускался.

Шаблон `templates/approved/КП LexRadar__v2_4.html` не изменён:
SHA-256 `7e352c2597ff272bcf25095ac442411da2fc291bdbb0559cf0dd116970de7b5e`, совпадает с №92.
Существующие untracked-файлы не редактировались и не удалялись.
AI-провайдеры подменены, транспортные тесты используют память; платных AI-запросов 0.
Push, merge, deploy не выполнялись. Качество реальных ответов AI не проверялось.
