# Задание №61 — восстановление подтверждённого пути OpenRouter

Дата: 2026-10-16. **READY для повторного использования исторического пути;
офлайн-совместимость проверена.** Доступность модели сейчас и успех следующего
платного запуска не проверялись. Причина timeout №60 не объявляется доказанной.

## Реальные успешные запуски

Найдены вне Git, в сохранённых локальных артефактах `/tmp`:

| Каталог `/tmp/` | Auditor A, UTC 2026-10-13 | Время, с | Input / output | USD | completed |
| --- | --- | ---: | --- | ---: | --- |
| lexradar-scz-ai-test-002 | 16:51:49.101115 | 40.69296843 | 29602 / 1369 | 0.0090278451 | false (B получил 429) |
| lexradar-scz-single-002 | 19:03:11.036261 | 69.12992829 | 29772 / 2314 | 0.0100437246 | true |
| lexradar-scz-single-003 | 19:16:21.971148 | 47.58943733 | 29868 / 1625 | 0.0093596607 | true |

Во всех трёх `requests.jsonl` у A: model=deepseek/deepseek-chat,
request_sent=true, status=success, attempt=1, известный usage/cost.
`analysis.json` содержит result Auditor A. В single-003 validation_notes=[];
это завершение клиентского pipeline, а не только запись генерации в OpenRouter.
`run.json` подтверждает mode=openrouter и разрешение передачи исходного пакета.
SHA пакета всех трёх:
`bc4ca144d26db1ecf787530a05637dc38716b5c91197b6f75f550c957290cf65`.
Prompt single-003 — a-v3,
SHA `8b40ff389184f325d472c171e526eceae5f1e3bde0488799f26ce9a763270f53`.

Выбранные метаданные и SHA256 исходных requests/analysis сохранены в
`tests/fixtures/openrouter_061/historical-evidence.json`. Тексты сайтов,
completion и старые разрешения туда не переносились. Это локальные свидетельства,
не новая независимая сверка API. Offline preflight со status=success, но
request_sent=false и cost=0, сознательно не принят за успешный API-запрос.

Пилот №39 не подходит как образец успеха: в
`work/first-pilot/run-039-7tj1yisr/paid/compact-analysis.json` был transport_error;
отчёты №40/42 фиксируют отсутствие пригодного клиентского ответа, несмотря
на сведения о генерации и списании. Compact dry-run и fake результаты также
не использованы как доказательство реального успеха.

## Историческая реализация и запрос

Изучена история provider.py: 702e00c → 183b86d → ccc08c7 → 53a9ad3 →
bff4e3e → b8a92e0 → b019041. До compact-профиля использовался общий
OpenRouterProvider. В ccc08c7 (13 октября) сохранены local OpenRouter изменения;
его классы OpenRouterProvider/OpenRouterTransport извлечены дословно в
`tests/fixtures/openrouter_061/transport_ccc08c7.py.txt`. Отдельная AST-проверка
подтвердила совпадение обоих классов с Git. Commit сделан позднее найденных
запусков: точный SHA исполнявшегося тогда worktree не записан. Это ближайшая
сохранённая реализация, согласующаяся с артефактами, а не доказанный deployment SHA.

Протокол этой реализации:

- POST `https://openrouter.ai/api/v1/chat/completions` через urllib Request,
  build_opener(NoRedirects), стандартный HTTPS/http.client; без SDK и SSE.
- Authorization: Bearer (значение не исследовалось); Content-Type: application/json.
  JSON сериализуется json.dumps(ensure_ascii=False).encode() в UTF-8.
- model из настроек; messages system/user; max_tokens из max_output_tokens;
  temperature=0, stream=false, n=1, response_format={type:json_object}.
- provider: allow_fallbacks=false, require_parameters=true,
  max_price={prompt: price cap, completion: price cap}; без only и reasoning.
- Общий monotonic deadline; socket.settimeout(remaining); последовательные
  HTTPResponse.read1(min(65536, remaining_size_allowance)) до завершения тела.
  Затем UTF-8 → json.loads всего envelope → проверка модели/finish_reason →
  choices[0].message.content и usage. Только затем JSON/схема результата аудита.

Найден `/tmp/lexradar-openrouter.json`: A deepseek/deepseek-chat,
max_output_tokens=6000, caps=$1/$2 за миллион; timeout=120, retries=0,
max_budget_usd=0.50, max_input_bytes=200000, max_response_bytes=1000000.
**Это текущий сохранённый конфиг, не неизменяемый snapshot запроса single-003.**
В run/requests/analysis нет его SHA или копии. Поэтому нельзя доказать, что
именно 6000, timeout=120 и retries=0 были значениями конкретного успешного POST.
Модель, usage, prompt SHA и время доказаны журналом; формат фиксированных
параметров и механизм чтения восстановлены по Git. Raw HTTP, framing,
выбранный upstream-провайдер, версия Python/TLS и proxy-настройки успешного
запуска не сохранены. Точного побайтового запроса со всеми параметрами нет;
синтетический тест не выдаётся за его replay.

## Отличия от single_auditor_live до исправления

| Поле | Успешный single-003 / исторический путь | №60 / текущий путь до правки |
| --- | --- | --- |
| Model ID | deepseek/deepseek-chat | deepseek/deepseek-v3.2 |
| Prompt | a-v3, SHA выше | single-a-v1, другой schema/system |
| Пакет | bc4ca144… | 42d33ebb… |
| Результат | полный envelope, usage и validated result | HTTP 200, 440 bytes, read timeout |
| Длительность | 47.589 с | 120.004 / 120.005 с |
| HTTP/JSON | POST, non-streaming JSON | тот же |
| Чтение | urllib/http.client.read1 | тот же механизм, с проверками полноты |
| Лимиты | кандидат-конфиг: 6000 tokens / 1 MB / $0.50 | 4000 tokens / 500 KB / $0.50 |
| Timeout/retries | кандидат-конфиг: 120 / 0 | запуск №60: 120 / 1 retry |
| Routing/caps | без only/reasoning, no fallback, $1/$2 | те же |

Изменение цикла в b019041: сокет сохраняется до чтения; выход при isclosed или
length=0; после выхода проверяется недополученный Content-Length. Старый цикл
ждал пустого read1 и заново обращался к response.fp.raw._sock. Это укрепление
полноты ответа, а не переход на другой транспорт. Его откат не требуется.
Исторический диагностический print исключений также не возвращается.

440 байт и HTTP 200 сами по себе не устанавливают содержимое ответа или
причину задержки. Найденные успехи доказывают существование рабочего пути;
они не доказывают причинную связь model ID с timeout №60.

## Минимальное исправление

В `src/lexradar/single_auditor.py` возвращён MODEL=deepseek/deepseek-chat.
DeepSeekAdapter и single_auditor_live продолжают использовать общий
OpenRouterProvider/OpenRouterTransport; нового HTTP-клиента нет. Описание
модели синхронизировано в docs/single-auditor-050.md.

Сохранены текущие ограничения: 4000 output tokens, caps $1/$2, $0.50,
500000 response bytes, 200000 input bytes, input token bound, 45/120 seconds,
1 retry, no fallback. Исторические более высокие лимиты не перенесены.
Бизнес-логика, промпты, schema/цитаты, approval, screening и учёт расходов не
менялись. Прежние отчёты/пакеты не переписаны. Старое разрешение single-003
не применяется к новому пакету.

## Офлайн-проверки

`tests/test_historical_openrouter_061.py`: три сравнительных сценария с настоящим
HTTPResponse над памятью, без сокетов. Исторический провайдер из fixture и
DeepSeekAdapter формируют одинаковые URL, POST, headers и байты JSON при
одинаковых сообщениях/настройках. Сверяются модель deepseek-chat, stream=false,
JSON object, caps/no-fallback, лимиты; совпадают непустые chunks read1,
собранное тело, content и usage. Проверены Content-Length, chunked и EOF.
В синтетическое тело включены 440 пробелов перед envelope; это проверка
совместимости, не утверждение о содержимом 440 байт №60.

```bash
env -u OPENROUTER_API_KEY PYTHONPATH=/tmp/lexradar-059-check:tests:src \
 /tmp/lexradar-029-venv/bin/python -m pytest -q \
 tests/test_historical_openrouter_061.py tests/test_transport_061.py \
 tests/test_provider_transport.py tests/test_diagnostics_060.py \
 tests/test_single_auditor_050.py tests/test_mvp_no_screening_055.py tests/test_auditors.py
```

**146 passed in 12.20s.** Ruff check и format check двух затронутых Python-файлов
прошли; git diff --check прошёл. Fixture AST совпадает с ccc08c7.
Использован существующий sitecustomize, запрещающий connect/connect_ex/getaddrinfo;
реальный ключ исключён из окружения тестов, адаптерам задан синтетический ключ.

API-запросы, чтение файла ключа, commit/push/merge/deploy не выполнялись.
Посторонние изменения сохранены. Предыдущая версия этого отчёта и исходный
изменённый single_auditor.py сохранены в `/tmp/lexradar-061-preserved/`.

Итог: **READY** — исторический маршрут восстановлен минимальной правкой,
совместимость протокола/чтения подтверждена офлайн. Реальный успех нового
запуска и точные несохранённые параметры старого POST этим не подтверждаются.
