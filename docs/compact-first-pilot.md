# Первый пилот compact-primary-v1

Платный пилот пока BLOCKED: требуется человеческий approval пакета; публичный
endpoint DeepInfra FP4 при проверке сообщил status=-2, доступность не подтверждена.
Описанные ниже команды платного запуска в задании №32 НЕ выполнялись.
`dry_run_ready` подтверждает локальные проверки, а не доступность провайдера.

Досье `examples/compact-first-pilot/dossier` основано на `make_dossier` и `pdf_bytes`
из существующих синтетических fixtures. Один условный публичный сайт `.example`,
пустая форма, три юридических PDF. Сбор живого сайта и отправка формы не выполнялись.
Юридические тексты учебные; ссылки HTML и сведения manifest не подтверждают
реальные маршруты. Отсутствие отдельных снимков формы явно отмечено.
Результат всегда HOLD / not_confirmed до независимого разбора.

Все команды выполнять из корня репозитория в окружении с установленным LexRadar.
В этой сессии Python: `/tmp/lexradar-029-venv/bin/python`.
Выходные каталоги должны быть новыми; сохраняйте старые результаты и ledger.

## Просмотр и подтверждение

Для удобного просмотра откройте локально
`examples/compact-first-pilot/review/index.html` двойным щелчком или через
«Открыть файл» браузера. Сервер и интернет не нужны. Сохраните рядом каталог
`dossier`: ссылки ведут на локальные оригиналы. HTML показывает точные system/user
сообщения, развёрнутый body, screening, цитаты, SHA и расчётный резерв.
HTML не создаёт approval и ничего не отправляет.

После просмотра выполните `sha256sum` из пункта 3. При совпадении можно сообщить:
«Пакет просмотрен; подтверждаю SHA-256
7b15886086ccf838a14a9a974ebee722f732fa7629478e14289a745392292acd».
Это подтверждение просмотра; платная отправка остаётся отдельным шагом.
Пересборка HTML: `python examples/compact-first-pilot/build_review.py`.

1. Просмотрите `review/packet.json` целиком: это точный исходящий user payload.
   Затем `review/primary.local.json`, `trace-map.local.json`, оригиналы в dossier.
   В packet используются keys/dictionary и ссылки `$`; развёрнутый body можно
   получить через `lexradar.primary.compact.unpack(prepare(...))`.
2. Проверьте `review/preflight.json`: suspicions=[]; эвристика не заменяет просмотр.
   Проверьте system prompt `messages_for_compact` в compact_pipeline.py: он также
   отправляется. Локальные primary/trace-map и бинарные PDF/PNG не отправляются.
3. Выполните:

```bash
sha256sum examples/compact-first-pilot/review/packet.json
```

Ожидаемый SHA-256:
`7b15886086ccf838a14a9a974ebee722f732fa7629478e14289a745392292acd`.
Пакет 29105 байт. Цитата e5:
`Synthetic privacy policy. Request topics are retained only for handling the request.`
Проверяется в privacy.pdf, его извлечённом тексте и trace-map.

4. После личного просмотра создайте локальную копию review/approval.json
   в `work/first-pilot/approval.json` (work игнорируется Git).
   Укажите approved=true, contents_reviewed=true, reviewer и реальный approved_at
   в ISO-8601 с часовым поясом; packet_sha256 должен точно совпасть.
   Не подписывайте шаблон до просмотра. Approval технически разрешает передачу;
   подтверждение только dry-run не является разрешением платного запуска.

## Dry-run

```bash
mkdir -p work/first-pilot
python -c 'from lexradar.auditors.compact_pipeline import main; main()' \
  examples/compact-first-pilot/dossier \
  --output work/first-pilot/dry-001 \
  --config examples/compact-openrouter-config.json \
  --as-of 2026-10-10T00:00:00+00:00 --mode openrouter \
  --allow-external-transfer --approval work/first-pilot/approval.json \
  --ledger work/first-pilot/site-ledger.json --dry-run
```

Те же аргументы доступны через CLI `lexradar compact-analyze`.

Ожидается dry_run_ready, SHA выше, sensitive_categories=[], резерв $0.00891178,
request_sent=false, budget_charge_usd=0. Ledger и lock при dry-run не создаются.
Резерв: (31353 × 0.26 + 2000 × 0.38) / 1000000; input bound включает
UTF-8 system/user и 1024 единицы запаса. Это верхняя оценка, не списанная сумма.
При BLOCKED смотрите blockers в compact-analysis.json; защиту не отключать.

## Один реальный запрос — отдельный разрешённый шаг

Перед запуском заново проверьте публичные
[endpoint metadata](https://openrouter.ai/api/v1/models/deepseek/deepseek-v3.2/endpoints).
Нужны модель deepseek/deepseek-v3.2, DeepInfra, tag deepinfra/fp4,
цены $0.26/$0.38 за миллион, response_format/reasoning/max_tokens и подтверждённая
доступность маршрута. Текущая проверка не подтверждает смысл status=-2.
Профиль цен действует до 2026-10-15 12:00 UTC (около суток), затем fail-closed.
`reasoning.enabled=false` соответствует [официальной документации](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens).
[Каталог моделей](https://openrouter.ai/api/v1/models) подтверждает reasoning
mandatory=false и default_enabled=false. Лимит конфигурации 2000, лимит endpoint 16384. Fallback выключен, retries=0,
require_parameters=true, response_format=json_object. Не менять маршрут наугад.

Получите отдельное разрешение на отправку; ключ OPENROUTER_API_KEY передайте
через безопасное окружение, не записывайте в файлы или историю команд.
Повторите dry-run с тем же ledger и убедитесь в совпадении пакета/approval.
После этого выполните ОДИН раз:

```bash
python -c 'from lexradar.auditors.compact_pipeline import main; main()' \
  examples/compact-first-pilot/dossier \
  --output work/first-pilot/paid-001 \
  --config examples/compact-openrouter-config.json \
  --as-of 2026-10-10T00:00:00+00:00 --mode openrouter \
  --allow-external-transfer --approval work/first-pilot/approval.json \
  --ledger work/first-pilot/site-ledger.json
```

Не перезапускайте автоматически даже при timeout. Сохраняйте один ledger
между всеми попытками: лимит $0.05 на сайт; pending/unknown/overrun и
недостаточный остаток блокируют дальнейшие попытки. Новый ledger обнулит историю
и потому не должен использоваться для обхода бюджета.

## Проверка результата и суммы

В paid-001/compact-analysis.json проверьте status=success, request_sent=true,
model, packet_sha256, result и usage. Цитаты проходят локальную валидацию;
production_outcome=HOLD, legal_status=not_confirmed, client_release_allowed=false.
Ошибка валидации не отменяет стоимости уже отправленного запроса.

Сопоставьте usage.cost_usd, budget_charge_usd и entries[].charge/state для
clinic.example в неизменяемой истории site-ledger.json с записью этого запроса
в OpenRouter Activity/Usage. При неизвестной цене сохраняется резерв и state=unknown;
это не доказательство реального списания. Не очищайте pending/unknown вручную.
Запись в OpenRouter и её request ID проверьте в интерфейсе аккаунта без повторной
генерации. Сверка счёта здесь не выполнялась: платных запросов не было.
