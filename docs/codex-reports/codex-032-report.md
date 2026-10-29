# Codex №32 — первый пилот LexRadar

Дата: 2026-10-14. Ветка: feat/compact-ai-audit-mvp, существующий Draft PR #28.
На начало текущей проверки worktree уже содержал изменённый compact_profile.py
и неотслеживаемые материалы задания №32. Они сохранены; копии отчёта, инструкции
и профиля перед правками находятся в /tmp/lexradar-032-preserved/. Изменения сохранены в текущем worktree; push/merge/deploy не выполнялись.

Итог: **BLOCKED для платного пилота**. Human approval точного пакета не получен.
Публичный endpoint DeepInfra FP4 возвращает status=-2; значение и доступность
маршрута не подтверждены. Защитные проверки не отключались.

## Досье и пакет

`examples/compact-first-pilot/dossier`: синтетическая публичная страница `.example`,
пустая форма обращения, организация без идентификаторов и три PDF (условия,
политика, согласие). Использованы существующие fixtures `test_primary.make_dossier`
и `lexradar.quality.fixtures.pdf_bytes`; PDF повторно извлечены штатным extractor.
Реальных клиентских данных и заполненных полей нет. Скриншот — синтетический пиксель;
отдельный снимок формы отсутствует и явно обозначен. Live collection не выполнялась;
метаданные acquisition остаются declared/unknown. HTML-ссылки `.html` и manifest
ссылки `.pdf` не подтверждают фактическую связность сайта; это ограничение fixture.

Схема compact-primary-v1, 29105 байт (лимит 40000).
SHA-256: `7b15886086ccf838a14a9a974ebee722f732fa7629478e14289a745392292acd`.
`sha256sum review/packet.json` совпал с prepare/report.
Screening: suspicions=[], sensitive_categories=[]; ни один паттерн не отключён.
Проверены целостность всех артефактов и воспроизводимость цитат.
Пример e5: `Synthetic privacy policy. Request topics are retained only for handling the request.`
Есть исходный PDF, извлечённый текст, page=1 и trace-map.
Все результаты HOLD / not_confirmed / client_release_allowed=false.

## Dry-run и бюджет

Полный локальный pipeline выполнен в mode=openrouter с dry_run=True,
allow_external_transfer=True и изолированным ledger.
`dry-run-without-approval/compact-analysis.json`: dry_run_blocked, blocker
`Human approval of exact packet SHA-256 required`.
Первый запуск с неподписанным шаблоном (approved_at=null) сохранён в `dry-run/`:
invalid_preflight. Он не является действительным approval.
Approval от имени пользователя не создавался. Пользователю запрошено подтверждение
просмотра review/packet.json, primary.local.json и trace-map.local.json;
ответ не подменяется синтетическим approval.

Резерв **$0.00891178** при лимите $0.05:
(31353 × $0.26 + 2000 × $0.38) / 1000000.
31353 — консервативный input bound UTF-8 system/user + 1024 overhead;
2000 — configured max_output_tokens. Резерв не означает списание.
На dry-run socket.connect и OpenRouterTransport.post были замещены запретом,
request_sent=false, usage=null, budget_charge_usd=0;
`/tmp/lexradar-032-pilot-ledger.json` и его lock не созданы.
Публичные web GET metadata были отдельной проверкой, без пакета/ключей.
Отсутствие сети относится к dry-run; metadata проверялись через web.
Реальных генераций и запросов к аккаунту для проверки баланса не было.

В новом `tests/test_first_pilot.py` тот же сохранённый fixture достигает
**dry_run_ready с синтетическим approval только в tmp_path теста**.
Тестовый approval не переносился в пример или платный пилот.
Тест проверяет цитаты/целостность, screening, отсутствие сети/ledger/lock/расходов,
а затем исчерпанный ledger: повторный dry-run BLOCKED и платный путь выбрасывает
BudgetExceeded до POST, ledger не изменяется. Это подтверждает работоспособность
gates, но не заменяет человеческое подтверждение.

## Официальные metadata

[OpenRouter endpoints](https://openrouter.ai/api/v1/models/deepseek/deepseek-v3.2/endpoints)
подтвердили model ID deepseek/deepseek-v3.2, provider DeepInfra, tag deepinfra/fp4,
цены per-token 0.00000026/0.00000038 ($0.26/$0.38 за миллион),
response_format, reasoning, max_tokens и max_completion_tokens=16384.
Профиль параметров/цен продлён до 2026-10-15 12:00 UTC — около суток для пилота;
это не сертификат доступности endpoint. Минимальный снимок полей и timestamp:
`examples/compact-first-pilot/openrouter-metadata.json`.

[Документация reasoning](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)
описывает отключение через enabled=false. Код формирует reasoning.enabled=false,
response_format=json_object и max_tokens=2000 (внутренний max_output_tokens).
Маршрут only=[deepinfra/fp4], allow_fallbacks=false, require_parameters=true,
retries=0. Поддержка параметров в metadata не доказывает поведение реального ответа.
Observed status=-2 сохранён без предположений о его значении; перед платным шагом
нужна повторная проверка доступности, без автоматического переключения маршрута.

## Созданные файлы

- `docs/compact-first-pilot.md`: просмотр, SHA, approval, dry-run, один отдельный
  платный запуск, проверка результата и счёта, сохранение ledger.
- `docs/codex-reports/codex-032-report.md`: этот отчёт.
- `tests/test_first_pilot.py`: acceptance fixture и бюджетные блокировки.
- `examples/compact-first-pilot/dossier/collection.json` и `artifacts/`:
  page-0001.html/.txt, synthetic-pixel.png, terms/privacy/consent.pdf/.txt.
- `examples/compact-first-pilot/review/`: packet, primary, trace-map, preflight,
  неподписанный approval.
- `examples/compact-first-pilot/dry-run/` и `dry-run-without-approval/`:
  сохранённые отчёты/preview и локальные snapshots заблокированных запусков.
- `examples/compact-first-pilot/openrouter-metadata.json`.

Изменён `src/lexradar/auditors/compact_profile.py`: только короткое окно проверки.

## Проверки

Финальный полный pytest: **961 passed, 31 failed, 92 errors** за 103.30 s.
Целевые compact/paid/first-pilot тесты: **39 passed** за 25.42 s.
Полный suite не зелёный; эти результаты не обозначаются как PASS.
Ruff check: passed. Ruff format --check: 143 files already formatted.
Новый acceptance test: 1 passed. Первый полный pytest:
960 passed, 31 failed, 92 errors за 82.82 s; ошибки локальных TCP sockets
PermissionError [Errno 1] Operation not permitted и браузерного окружения sandbox.
Повторный полный запуск использовал имеющийся Chromium в
PLAYWRIGHT_BROWSERS_PATH=/tmp/lexradar-029-browsers; sandbox не обходился.

Оставшиеся препятствия: human approval, подтверждение доступности маршрута,
полный зелёный pytest в разрешённом окружении с localhost/browser,
отдельное разрешение платного шага. Истечение профиля потребует новых публичных
metadata. Публикация в PR #28 оставлена отдельному разрешённому шагу.

## Повторная проверка текущего worktree

Официальный публичный GET endpoints повторно подтвердил указанные цены,
параметры и status=-2. SHA пакета остался прежним.
Повторный полный dry-run с запретом socket.connect и OpenRouterTransport.post
сохранён в `/tmp/lexradar-032-recheck-yb2xa8hs/dry-run`: dry_run_blocked,
invalid_preflight из-за неподписанного шаблона approval (approved_at=null).
request_sent=false, budget_charge_usd=0; ledger и lock отсутствуют.
Повторные целевые тесты: 39 passed за 27.67 s. Ruff check и format прошли.

Последний полный pytest текущей проверки: **960 passed, 32 failed, 92 errors**
за 108.29 s, лог `/tmp/lexradar-032-current-pytest.log`. Эта проверка является
актуальным результатом; предыдущие прогоны выше сохранены как история.
Помимо браузерных/localhost ограничений упал
`test_resume_stops_identified_orphan_worker`; полный suite остаётся препятствием.
[Каталог моделей](https://openrouter.ai/api/v1/models) повторно подтвердил
reasoning mandatory=false и default_enabled=false; цены берутся именно
из закреплённого endpoint, а не агрегированного каталога моделей.

## Публикация результатов задания №32

По отдельному разрешению пользователя материалы подготовлены для существующего
Draft PR #28, ветка `feat/compact-ai-audit-mvp`. Проверка ограничена изменениями
задания №32: отчёт, инструкция, синтетические fixtures, `test_first_pilot.py`
и изменение `compact_profile.py`. Просмотрены текстовые данные JSON/HTML,
содержимое и metadata трёх PDF; проверены шаблоны секретов и SHA-256 пакета.
Секретов и реальных клиентских данных в публикуемых файлах не обнаружено.
Approval в fixtures остаются неподписанными. Исторические результаты тестов
выше сохранены без исправлений; новые тесты и аудит на этапе публикации
не запускались. Платные запросы и отправка досье в OpenRouter не выполнялись.
