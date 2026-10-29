# Задание №55 — MVP_NO_SCREENING

Дата: 16.10.2026. **READY для первого отдельно разрешённого тестового аудита DeepSeek A.** Досье технически готово; разрешение на передачу не создано, аудит не запускался. До отдельного человеческого подтверждения точного пакета и разрешения внешнего вызова передача остаётся заблокированной.

## Изменения

- `auditors/preflight.py`: явный keyword-only `mvp_no_screening=True` пропускает только вызов блокирующего screening. По умолчанию `False`; прежняя блокировка сохранена. `screen`, шаблоны, категории и классификация данных не изменены. SHA256 payload и весь контракт approval проверяются в обоих режимах.
- `auditors/orchestrator.py`: флаг передаётся в первоначальную авторизацию и повторно перед каждой платной попыткой. Разрешён только с неизменённым `single_auditor.profile()` и `single_auditor.messages_for`. В `analysis.json/limitations` и `run.json/personal_data_transfer_check` записывается: **«MVP_NO_SCREENING: проверка передачи персональных данных отключена»**; в run также сохраняется boolean `MVP_NO_SCREENING`.
- Новый `single_auditor_live.py`: отдельная команда реального Single A, обязательные `--packet-approval` и `--allow-external-transfer`, явный `--mvp-no-screening`. Без последнего screening включён. Переменные окружения не включают обход автоматически. Существующий CLI `single_auditor` остаётся офлайн.
- Целостность досье, воспроизведение текста, SHA256, размер входа/ответа, фиксированный профиль DeepSeek, OpenRouter transport и контроль модели/ошибок, бюджет, повторы и подтверждение конкретного пакета сохранены. `automatic_send_allowed=false`; команда сохраняет только локальный analysis, не генерирует и не отправляет клиентские документы.

Посторонние изменения оставлены на месте. Из ранее существовавших файлов изменены только `src/lexradar/auditors/preflight.py`, `src/lexradar/auditors/orchestrator.py` и документация `docs/single-auditor-050.md`. Досье №54 не редактировалось; пофайловые SHA256 сверены с его прежним `improved-SHA256SUMS.txt`.

## Проверка улучшенного досье

Путь: `runs-054/doktstom-improved/dossier`. Локальная проверка выполнена с запрещёнными socket connect/DNS, без создания AI-провайдера. Сохранены `runs-055/verify_local.py`, `runs-055/readiness.json`, `runs-055/tests.log`; каталог игнорируется через собственный `.gitignore`.

| Проверка | Результат |
|---|---|
| Manifest SHA256 | `0e7f728a9f130d4f7e46b8c0e0ca30cefb18d71734b6bfb12468b26bb55d46b0` |
| Payload SHA256 | `42d33ebbf0e329b6ea76bb04bdcb9f844877a8f046c18610ce378ca8ab7f06f9` |
| Целостность / прежние пофайловые SHA256 | ошибок нет / совпадают |
| Payload | 34319 байт при лимите 200000 |
| System + user + overhead 1024 | 46476 байт при границе 240000 |
| Резерв одной / двух попыток | $0.054476 / $0.108952 при бюджете $0.50 |
| Режим выключен, approval отсутствует | прежний отказ `Potential sensitive data: transfer blocked; review/redact and repeat` |
| Режим включён, approval отсутствует | отказ `Human approval of exact packet SHA-256 required` |

Шесть прежних screening-срабатываний воспроизводятся без изменений, но при явном включении режима не блокируют аудит. Исключения по категориям не добавлялись. Сохраняются ограничения №54: производный manual HTML import, частичное исходное доказательство, отсутствие нового сбора сайта и независимого подтверждения происхождения/актуальности. READY означает техническую готовность тестового inference, а не юридическую полноту досье или готовность отправлять заключение клиенту. Доступность модели, актуальные тарифы, ключ и баланс офлайн не проверялись; профиль использует прежние защитные потолки.

## Офлайн-проверки

```bash
PYTHONPATH=src /tmp/lexradar-029-venv/bin/pytest -q \
  tests/test_mvp_no_screening_055.py tests/test_auditors.py \
  tests/test_single_auditor_050.py tests/test_preflight_dates.py \
  tests/test_prepare_doktstom_054.py
```

**127 passed in 14.10s**. Новые тесты проверяют оба режима, обязательность approval и внешнего разрешения, сохранение SHA256/целостности/размера, ограничение Single A и запись предупреждения в локальные отчёты с запретом автоматической отправки. Сеть в новых тестах запрещена. Ruff check и format --check четырёх затронутых Python-файлов прошли. CLI проверен только через `--help`.

Повторная локальная проверка досье, без передачи:

```bash
PYTHONPATH=src /tmp/lexradar-029-venv/bin/python runs-055/verify_local.py
```

## Точная команда следующего реального запуска

Выполнять из `/home/golkonst/src/LexRadar-compact` **только после отдельного разрешения** передачи этого пакета через OpenRouter в `deepseek/deepseek-v3.2`, бюджета $0.50 и появления человеческого approval по указанному пути. Выходной каталог должен отсутствовать. `OPENROUTER_API_KEY` должен быть настроен штатным способом; команда не устанавливает и не выводит ключ.

```bash
PYTHONPATH=src /tmp/lexradar-029-venv/bin/python -m lexradar.single_auditor_live \
  runs-054/doktstom-improved/dossier \
  runs-055/doktstom-deepseek-a-001 \
  --packet-approval runs-055/doktstom-packet-approval.json \
  --allow-external-transfer \
  --mvp-no-screening
```

`runs-055/doktstom-packet-approval.json` **не создан**. Он должен соответствовать существующему `PacketApproval`: SHA256 payload из таблицы, `approved=true`, `contents_reviewed=true`, непустой reviewer и фактическое timezone-aware approved_at не в будущем. Это описание существующего контракта, не разрешение на передачу. После изменения payload требуется новое подтверждение его SHA256.

Реальных API-вызовов, создания разрешений, отправки клиентам, commit, push, merge и deploy не было. Прочитан skill cloud-environment-runtime; инструмент environment_status в этой сессии не предоставлен, сеть и секретные файлы не использовались.

**READY:** прежний screening-блокер снимается исключительно явным MVP_NO_SCREENING; целостность, размер и локальный резерв бюджета подходят. Реальная передача ожидает отдельного человеческого разрешения и approval конкретного пакета.
