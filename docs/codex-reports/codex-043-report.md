# Codex №43 — PermissionError в metadata-проверке

Дата: 15.10.2026. Выполнено от uid=1000, gid=1000 (golkonst).
POST, платная генерация и реальные GET к OpenRouter не выполнялись.

## Причина и воспроизведение

Изучены scripts/run_paid_pilot_042.py, scripts/run_paid_pilot_039.py,
реальный WORK, порядок операций и отчёт №42. REPO определяется через
Path(__file__).resolve(), а не текущий каталог запуска.

Прежний WORK: `/home/golkonst/src/LexRadar-compact/work/second-pilot`.
Он отсутствовал. Его родитель `work` принадлежит nobody:nogroup (65534:65534),
имеет видимые права 0775. У процесса есть supplementary group nogroup, однако
os.access(work, W_OK|X_OK) возвращает False, а фактический mkdir возвращает
PermissionError, errno=13 (EACCES). Следовательно, видимые POSIX-биты не описывают
весь эффективный контроль доступа этого окружения. Нельзя приписывать отказ
только отсутствию групповых прав или утверждать, что это ошибка API.
Точный дополнительный механизм отказа из доступных данных не установлен.

Старый порядок: prepare (успешный локальный dry-run), затем mkdir WORK,
проверка symlink, durable_claim, чтение ключа и GET, запись evidence.
Наблюдаемый отказ происходил на mkdir, до маркера, ключа и GET.
Внешний обработчик скрывал все подробности за STOP (PermissionError).
Отчёт №42 предлагал обычное окружение пользователя как обход, но это не
исправляло недоступный эффективный путь в текущем окружении.

## Исправления

Фиксированный новый WORK:
`/home/golkonst/src/LexRadar-compact/work/first-pilot/second-pilot`.
Родитель first-pilot принадлежит golkonst:golkonst и реально доступен на запись.
Новый каталог создан от golkonst с 0700. Это постоянное расположение, а не
автоматический fallback при ошибке доступа. Старый путь не удалён и не изменён:
его наличие (включая symlink) блокирует metadata и execute для ручной проверки,
чтобы перенос не обходил прежние маркеры. Существующие first-pilot ledger,
approval и attempt продолжают использоваться на исходных путях.

Добавлен --check-metadata: настоящий prepare и только локальная проверка
путей, существующих evidence/маркеров, возможности записи и чтения/формата ключа.
Он не создаёт claim и не выполняет GET. Временная проба записи удаляется штатно.
Symlink проверяются до mkdir. Чтение ключа и проверки вывода перенесены до claim.
Evidence открывается эксклюзивно до GET: ошибка создания файла не расходует
сетевую попытку. Постоянный O_EXCL claim с fsync остаётся перед GET и сохраняется
при любой дальнейшей ошибке; evidence записывается с flush/fsync.

Диагностика локальных ошибок содержит фиксированный этап, тип, errno и
заранее известный локальный путь. Произвольный текст исключений не выводится,
ключ не печатается. Ошибки GET/proposal сохраняют тип/errno, для вложенной
сетевой причины — только её тип/errno; HTTP body, заголовки и repr исключений
не сохраняются. Неизвестные ошибки верхнего уровня по-прежнему выводят только тип.

## Проверки

- Локально воспроизведён EACCES старого mkdir без сети.
- `pytest -q tests/test_pilot_042.py tests/test_provider_transport.py tests/test_auditors.py tests/test_compact_paid.py tests/test_paid_pilot_039.py`: **124 passed**.
- Ruff для scripts/run_paid_pilot_042.py и tests/test_pilot_042.py: All checks passed.
- Новые тесты: локальный preflight без GET/claim, приватные права каталога,
  все три существующих маркера/evidence, legacy и symlink, безопасная диагностика
  EACCES, недоступный ключ до claim/GET, сохранение claim после имитированной
  ошибки и запрет повторного вызова. Все сетевые вызовы в тестах подменены.
- Реальный `--check-metadata`: metadata_preflight=ready; no GET or claim,
  request_sent=false, prepared_blocked / dry_run_blocked из-за unknown expense.
- Новый WORK после проверки пуст, metadata/paid маркеров не создано.

SHA256 до и после совпали:

| Файл | SHA256 |
| --- | --- |
| site-ledger.json | c67f08ea3c12ff5916b15f16c0658bb4e7ce8172be5217cc59880831a89981fa |
| approval-038.json | 3ad8f79918ad6fc057f4f3f4f22cce75fbc8099f2486910f715e808be45f66d8 |
| attempt-039.json | 9b90b5084c424bb053713d729d8fe170aa18a2219edefc3f9610232d12035db4 |

Маркеры не удалялись, ledger и approval не изменялись. Остальные существующие
изменения рабочего дерева не переписывались. Unknown, HOLD и необходимость
отдельного решения о сверке/новой оплате сохраняются. Сетевой успех этим
исправлением не подтверждён; повторный GET в этом задании запрещён.

Следующий безопасный шаг — повторить только локальную проверку:

```bash
/tmp/lexradar-029-venv/bin/python scripts/run_paid_pilot_042.py --check-metadata
```
