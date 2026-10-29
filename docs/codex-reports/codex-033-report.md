# Codex №33 — готовность первого пилота

Дата проверки: 2026-10-14. Текущий worktree, ветка
`feat/compact-ai-audit-mvp`, Draft PR #28. Итог: **BLOCKED** для реального пилота.
Локальный просмотр подготовлен; воспроизводимых дефектов compact pipeline
не обнаружено. Архитектура, модель, маршрут, screening и старые тесты не изменены.

## OpenRouter

Выполнены только публичные GET через web, без API-ключа, досье и POST.
[Metadata модели и endpoints](https://openrouter.ai/api/v1/models/deepseek/deepseek-v3.2/endpoints)
подтверждают `deepseek/deepseek-v3.2`, DeepInfra, `deepinfra/fp4`, quantization fp4,
`status=-2`. Снимок сохранён в
`examples/compact-first-pilot/openrouter-metadata-033.json`.

Тариф endpoint: input $0.26, output $0.38, cache read $0.13 за миллион токенов
(0.00000026 / 0.00000038 / 0.00000013 за токен). В supported_parameters
присутствуют response_format, reasoning, max_tokens, structured_outputs;
max_completion_tokens=16384. Конфигурация использует max_tokens=2000.
JSON mode `response_format={type:json_object}` описан в
[официальном Chat API](https://openrouter.ai/docs/client-sdks/python/api-reference/chat).
[Reasoning documentation](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)
описывает единый reasoning и отключение enabled=false; это не exclude=true,
которое лишь скрывает reasoning. Endpoint заявляет reasoning, но успешное выполнение
enabled=false и JSON mode реальным ответом не проверено: запрос запрещён заданием.
Metadata подтверждают заявленную поддержку, а не работоспособность.

**Официального объяснения числового status=-2 не найдено** в проверенных
[reference endpoints](https://openrouter.ai/docs/api/api-reference/endpoints/list-all-endpoints-for-a-model),
[provider routing](https://openrouter.ai/docs/guides/routing/provider-selection),
reasoning docs и поиске по официальному домену. Reference показывает status=0
в примере, без таблицы кодов. Не интерпретируем -2 как конкретную причину отказа;
доступность закреплённого маршрута остаётся неподтверждённой.

Кандидат для отдельного решения при недоступности DeepInfra: та же модель,
`siliconflow/fp8`, observed status=0, input $0.259 / output $0.80 за миллион,
cache read $0.14; max_completion_tokens=147456; response_format, reasoning,
max_tokens присутствуют. Все эти поля взяты из того же публичного снимка.
Status=0 не заменяет проверку реального запроса. Альтернатива **не активирована**;
её резерв для текущих границ был бы $0.009720427. GMICloud дешевле, но metadata
не заявляют response_format, поэтому его не предлагаем для JSON mode.
Профиль не продлевался: существующий срок 2026-10-15 12:00 UTC;
после истечения требуется новая публичная проверка.

## Полный pytest и причины ошибок

Окружение: `/tmp/lexradar-029-venv/bin/python` (Python 3.13), зависимости установлены;
Chromium 1248 и headless shell уже присутствуют в `/tmp/lexradar-029-browsers`.
Команда:

```bash
PLAYWRIGHT_BROWSERS_PATH=/tmp/lexradar-029-browsers \
  /tmp/lexradar-029-venv/bin/python -m pytest -q
```

Результат: **961 passed, 31 failed, 92 errors**, 102.05 s.
Полный лог: `/tmp/lexradar-033-pytest.log`.

- 92 errors — fixtures collector/crawl_policy не могут создать TCP socket:
  PermissionError [Errno 1] Operation not permitted. Независимая проверка
  socket.socket() подтверждает запрет до bind/listen на 127.0.0.1.
- 31 failed — tests/test_quality.py: 19 reference scenarios останавливаются
  с TargetClosedError; остальные проверки получают отсутствие generated dossier,
  verification/analysis/decision файлов и итог failed вместо ожидаемого результата.
  Это каскад остановки общей quality fixture до штатного pipeline.
- Независимый offline Chromium launch (без сайта) воспроизводит TargetClosedError:
  FATAL content/browser/sandbox_host_linux.cc:41, shutdown: Operation not permitted (1).
  Лог `/tmp/lexradar-033-browser.log`. Бинарник найден; проблема не в его отсутствии.
- `test_resume_stops_identified_orphan_worker`, упавший в последней проверке №32,
  в текущем полном прогоне прошёл. Воспроизводимый дефект не подтверждён.

**Полный прогон с разрешённым localhost не удалось выполнить**: действующая
политика workspace sandbox запрещает sockets, approval policy=never.
Другого разрешённого окружения в этой сессии нет. Ограничения не обходились.
Эти результаты не являются зелёным suite и не доказывают отсутствие дефектов
в путях, которые окружение не позволило выполнить.

Повторные compact/paid/first-pilot тесты: **39 passed**, 23.95 s;
лог `/tmp/lexradar-033-compact.log`. Они проверяют saved fixture, целостность,
цитаты, gates, сухой запуск, отсутствие POST и бюджетные блокировки.
Ruff check: passed. Ruff format --check: 144 files already formatted
(новый generator отформатирован). Старые тесты не менялись.

## Исправления и HTML

Ошибки кода не исправлялись: относящиеся к compact pipeline воспроизводимые
дефекты не найдены. Добавлен удобный локальный просмотр, устраняющий неудобство
ручного чтения dictionary/keys:

- `examples/compact-first-pilot/review/index.html` — статический HTML без JS,
  сервера и внешних ресурсов; CSP запрещает активное содержимое и сетевые ресурсы.
- `examples/compact-first-pilot/build_review.py` — воспроизводимая локальная
  пересборка только сохранённого синтетического fixture, без API и approval.
- `docs/compact-first-pilot.md` — короткая инструкция просмотра и подтверждения SHA.

Генератор сверил точные байты packet.json с prepare(), штатно проверил цитаты
и SHA исходных артефактов. HTML содержит оба точных сообщения, развёрнутый body,
все категории screening с количеством совпадений (0), исходные цитаты,
локальные ссылки, страницы, basis, SHA артефактов и полную трассировку.
HTML parser подтвердил существование всех локальных ссылок и отсутствие
script/iframe/img/object/embed/form и внешних href/src. Визуальная проверка
браузером заблокирована указанной ошибкой Chromium.

Пакет: 29105 байт; SHA-256
`7b15886086ccf838a14a9a974ebee722f732fa7629478e14289a745392292acd`.
Резерв **$0.00891178** = (31353 × 0.26 + 2000 × 0.38) / 1000000,
консервативный input bound UTF-8 system/user + 1024 overhead. Это оценка,
не фактические токены или списание. Отправка не выполнялась, расход задания $0.
Содержимое получено из синтетического fixture №32; секретов и клиентских данных
в новых материалах нет. Адреса .example показаны текстом; HTML не загружает их.

## Подтверждение и точные блокировки

Откройте index.html локально, раскройте system/user/body и проверьте оригиналы.
Из корня выполните:

```bash
sha256sum examples/compact-first-pilot/review/packet.json
```

После личного просмотра подтвердите полученный SHA сообщением
«Пакет просмотрен; подтверждаю SHA-256 [полученный SHA]».
Human approval не создавался: существующий шаблон остаётся неподписанным.
Подтверждение просмотра и разрешение платной отправки — отдельные действия.

Готовность **BLOCKED** по следующим причинам:

1. Доступность deepinfra/fp4 не подтверждена; status=-2 без найденной официальной расшифровки.
2. Полный pytest не зелёный; требуется повтор в разрешённом окружении с localhost
   и работающим Chromium, включая quality/collector/crawl_policy.
3. Нет пользовательского approval точного пакета после личного просмотра.
4. Реальный платный шаг не разрешён текущим заданием и не выполнялся.
   При последующем запуске также нужна действующая проверка профиля цен/параметров.

Результаты сохранены только в текущем worktree. Main, merge, deploy, push,
публикация в PR #28, отправка досье и платные запросы не выполнялись.
