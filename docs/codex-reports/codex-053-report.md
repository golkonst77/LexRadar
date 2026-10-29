# Задание №53 — подготовка реального аудита doktstom.ru

Дата проверки: 16.10.2026. **NOT READY**: выбранное досье цело и помещается
во вход DeepSeek A, но `authorize()` блокирует передачу по четырём категориям
screening; извлечены только 399 символов, политика не поступает модели.
В текущем процессе отсутствует `OPENROUTER_API_KEY`. Approval не создавался.

## Метод и сохранность

Изучены docs/single-auditor-050.md, отчёты №50 и №52, а также №36;
collector/runner, static_renderer, artifacts, reading/text_integrity,
auditors/evidence, preflight, budget, provider, models, validator,
orchestrator и single_auditor. Применён skill cloud-environment-runtime:
инструмент environment_status недоступен, сетевые действия не требовались.
Поиск локальных collection.json выполнен в `/home/golkonst/src` и `/tmp`;
тестовые fixtures и временные копии репозитория не принимались за реальные сборы.
Найдено десять реальных сохранённых досье целевого домена.

Проверки выполнены существующим `/tmp/lexradar-029-venv/bin/python`,
`PYTHONPATH=src`. Проверяющий процесс запрещал DNS и socket.connect.
Для каждого кандидата применены CollectionResult, verify_integrity,
load_packet(200000), messages_for, screen, authorize(packet, None),
локальный расчёт Budget.reserve. Провайдер не создавался; complete/analyze
с платным режимом не выполнялись. Проверено только наличие переменной ключа,
без вывода значения, чтения .env, секретных файлов или профилей credentials.

Исходники и прежние незакоммиченные изменения не редактировались. Создан только
этот отчёт в репозитории; диагностический скрипт и результаты оставлены в
`/tmp/lexradar-053/`. Нового обхода, внешних сервисов, AI API, платных запросов,
отправки, commit/push/merge/deploy и изменения main не было.

## Кандидаты и выбор

Пути ниже относительно `/home/golkonst/src/LexRadar-compact`, кроме абсолютных.
Все десять проходят локальную целостность и load_packet с лимитом профиля.
Нулевая ошибка integrity сама по себе не означает полноту доказательств.

| Досье | HTML / текст / скриншоты | Формы | Текст, символов | Пакет, байт |
|---|---|---:|---:|---:|
| work/mvp-doktstom-001/dossier | 1 / 1 / 0 | 0 | 399 | 5169 |
| runs-036/doktstom-saved-result/dossier | 1 / 1 / 0 | 0 | 399 | 5169 |
| runs-036/doktstom-saved-final/dossier | 1 / 1 / 0 | 0 | 399 | 5169 |
| runs-036/doktstom-live-001/dossier | 1 / 1 / 0 | 0 | 399 | 5169 |
| runs-036/doktstom-live-002/dossier | 1 / 1 / 0 | 1 | 399 | 6406 |
| runs-036/doktstom-final/dossier | 1 / 1 / 0 | 1 | 399 | 6406 |
| runs-036/doktstom-reviewed/dossier | 1 / 1 / 0 | 1 | 399 | 6406 |
| runs-036/doktstom-result/dossier | 1 / 1 / 0 | 1 | 399 | 6406 |
| /home/golkonst/src/LexRadar/work/smoke-run-001/sites/2c83a7d3a1ff2a062531/attempt-0001/dossier | 0 / 0 / 0 | 0 | 0 | 3375 |
| /tmp/lexradar-first-test/sites/2c83a7d3a1ff2a062531/attempt-0001/dossier | 1 / 1 / 2 | 1 | 399 | 7139 |

**Выбрано наиболее полное по артефактам досье:**

```text
/tmp/lexradar-first-test/sites/2c83a7d3a1ff2a062531/attempt-0001/dossier
```

У него есть оба изолированных скриншота; у более новых №36 то же исходное HTML
и тот же короткий текст, без скриншотов. Выбор не означает готовности к запуску.
Путь в /tmp непостоянен: до будущей работы сохранить побайтовую архивную копию,
проверив те же хеши; не публиковать сырые материалы в Git.

SHA256 файла `collection.json`:

```text
6b62626ce9f29b8d8277b3b996d3fe0c5a26fc3c22d5e69a22af8a39095a4473
```

SHA256 **фактического исходящего текста load_packet**, с текущим кодом и defaults:

```text
4b9a72b0b0277e2773c5e6cd3557246fb632535b99f64df35461b9bb0adc1b8a
```

Это разные хеши; approval привязывается ко второму. Изменение кода извлечения,
метаданных или досье требует повторного построения, просмотра и нового approval.

## Целостность и содержимое выбранного досье

Сбор начат 13.10.2026 15:08:29.837899 UTC. Одна страница, ноль PDF/документов,
одна обнаруженная форма. Manifest warnings пуст; evidence `page-0001` имеет
status=complete. Это статус наличия артефактов, а не полного прочтения.

| Артефакт | Байт | SHA256 |
|---|---:|---|
| HTML | 153454 | 3257dc3e6b801daf640e8dc52c8a219ba393b3a47f8dfdab6c9d47f3a43aa8fa |
| text | 637 | e738903b34bcffc3bec8069b3990f51a78a821afa4c01e3a59a901fcfb7b8b5d |
| screenshot | 44182 | a2d17fc592d6a59e1063b9e6cae87b804f63d3599b2b90939681db28687162b3 |
| form_screenshot | 20430 | c458ff3aab859df80df523e7f4f4033472d9c96e4761fbc2bb91644f45ed777f |

Все четыре размера и SHA256 совпадают с manifest. Проверены ссылки evidence,
безопасность путей, обязательные артефакты и регистрация скриншота формы;
текст совпадает с артефактом и воспроизводится из HTML.

HTML содержит 140159 символов и два вхождения «Политика конфиденциальности»;
вхождений `.pdf` нет. Это не доказательство полноты политики или отсутствия
PDF на сайте. В исходящем тексте политика отсутствует. Отдельных юридических
документов нет. Визуальное и юридическое чтение скриншотов в задании не проводилось.

После recheck_text: provenance=reproduced, extraction_state=partial,
text_coverage_complete=false, text_truncated=false, supplied_chars=399.
Причина — `Mismatched HTML closing element; suffix withheld`.
Дополнительно: `Static UTF-8 HTML text; CSS visibility/layout and JavaScript
not examined`, `Extraction is not legal research`.
399 символов — не лимит запроса, а результат строгого извлечения до ошибки HTML.

Collector limits: 10 страниц, 5 документов, 5 млн байт на файл, 50 млн суммарно,
10 секунд на операцию, 3 redirect, 200 ссылок на страницу, 100 страниц PDF.
Лимиты не превышены. Оригинал не аутентифицирует издателя; метаданные времени
являются локальным наблюдением. Форма не отправлялась, работа JS/согласий,
актуальность сайта и нормы не проверялись.

## Контракт, screening и бюджет

load_packet отдаёт JSON с untrusted_collector_data и analysis_limitations.
Он отправляет текст и метаданные, но не HTML/PDF binaries и не пиксели.
Recovery первичного compact-анализа №36 не используется messages_for Single A;
его значительно более богатые факты нельзя приписывать этому пакету.
Ответ ожидается как один AIAuditorResult с auditor=A, без A/B wrapper.

screen выбранного пакета дал четыре срабатывания (значения не публикуются):

- birth_date: `$.untrusted_collector_data.pages[0].forms[0].submission_context`;
- email, phone_or_identifier, person_name: `$.untrusted_collector_data.pages[0].text`.

Это эвристические категории, а не установленное наличие медицинской тайны.
Однако authorize проверяет screen **раньше approval** и неизменно выдаёт
`Potential sensitive data: transfer blocked; review/redact and repeat`.
Даже approved=true с правильным SHA не разрешит текущий пакет. Отключать screen,
подменять функции или делать произвольную обрезку payload нельзя.

Профиль фиксирует deepseek/deepseek-v3.2, single_auditor=true; поле auditor_b
нужно схеме, но B не запускается. Лимиты: $0.50, prompt $1/млн,
completion $2/млн, max_tokens=4000, timeout=45 s, retries=1,
max_backoff=1 s, packet=200000 bytes, response=500000 bytes.
Текущий пакет 7139 байт; сообщения с framing overhead 1024 дают консервативную
границу 19296 токенов/UTF-8 байтов, меньше 240000 у адаптера.
Резерв попытки: `(19296 × 1 + 4000 × 2) / 1000000 = $0.027296`.
Две неизвестные по цене попытки удержат $0.054592, меньше $0.50.
Один аудит может включать до двух HTTP-попыток; менять retries в profile нельзя:
DeepSeekAdapter проверяет точное совпадение конфигурации.

Budget резервирует до каждой попытки, неизвестная стоимость не возвращает
резерв, известная usage.cost корректирует его. Обнаруженный перерасход
останавливает следующие попытки, но не отменяет уже начисленную провайдером
стоимость. Это локальный лимит с защитными price caps, не абсолютная гарантия
внешнего списания. Бюджет создаётся заново для каждого analyze: не перезапускать
команду автоматически и не считать $0.50 общим лимитом нескольких запусков.
Для строгой финансовой гарантии нужен отдельный лимит у провайдера; наличие
такого лимита здесь не проверялось.

## Возможность реального вызова

Архитектурно готов: существующий analyze с profile(), messages_for и
DeepSeekAdapter выполняет только A, без изменения исходников. Offline CLI
`python -m lexradar.single_auditor` предназначен для replay, платного флага нет.
Общий CLI analyze без инъекции messages_for не является точным маршрутом №50.

Нужны: проверенное досье, чистый screen, человеческий просмотр точных байтов,
packet approval с этим SHA, отдельное явное разрешение OpenRouter и $0.50,
OPENROUTER_API_KEY из защищённой среды, новый каталог результатов, доступ к
https://openrouter.ai/api/v1/chat/completions и возможность оплаты модели.
Модель/доступность/актуальный тариф/баланс/сетевой доступ не подтверждены:
по заданию внешние запросы запрещены. Ключ отсутствует в текущем процессе;
секретные файлы не искались. Значения конфигурации модели подтверждены кодом,
а не актуальным каталогом OpenRouter.

Payload адаптера: temperature=0, stream=false, n=1,
response_format=json_object, provider.allow_fallbacks=false,
require_parameters=true, max_price.prompt=1, completion=2.
Конкретный upstream provider не закреплён; compact_paid-настройки сюда
не переносятся. Модель ответа должна точно совпасть; finish_reason stop/end_turn.

## Минимальное улучшение без переписывания сборщика

1. Сохранить выбранный оригинал и manifest неизменными в устойчивом закрытом
   хранилище. Локально изучить полный HTML, форму и встроенную политику;
   использовать существующий SavedMarkup(recover=True)/primary как навигацию
   с частичными диапазонами, не объявляя их полным чтением Single A.
2. Подготовить отдельную явно производную, обезличенную копию материалов
   с использованием существующего контракта `manual_html_import`
   (`collector/provenance.py`, `collector/models.py`). Готового CLI импорта
   в текущем дереве нет: потребуется небольшой отдельный локальный скрипт
   подготовки производного досье, без переписывания сборщика. Сохранить происхождение,
   исходный хеш, изменения и ограничения; не выдавать исправленную HTML-копию
   за оригинальный HTTP capture. Удаление идентификаторов должно охватывать
   также submission_context и метаданные. Простая правка page.text не годится:
   load_packet воспроизводит текст из HTML и проверяет SHA256.
3. Исправление структуры HTML в производной копии или выделение нужного
   локального документа требует явной маркировки преобразования. Не снимать
   partial/unknown вручную. Если существующий импорт не обеспечивает нужное
   происхождение и воспроизводимость, текущих механизмов недостаточно:
   нужен отдельный согласованный способ подготовки входа, а не обход проверки.
4. Повторить load_packet/screen/messages/budget; просмотреть юридически полезный
   текст, заново зафиксировать оба SHA и лишь затем просить разрешение нового
   пакета. PDF из сети в рамках №53 не получать; отсутствие PDF не исправлять
   вымышленными документами. Дополнительный сбор возможен только отдельным заданием.

Сейчас ни улучшенная копия, ни новый хеш, ни разрешение не созданы.

## Точные команды будущего запуска (не выполнялись)

Ниже зафиксирован **нынешний конкретный пакет**. Первый блок безопасен и
должен остановиться на screening. Следующие блоки приведены для будущего
оператора; не запускать их при NOT READY. После улучшения досье необходимо
заменить ROOT, оба SHA и провести новый просмотр/разрешение: старый хеш не
может разрешать новую копию. Проверка screening не должна удаляться.

```bash
cd /home/golkonst/src/LexRadar-compact
export PYTHONPATH=src
export LR53_ROOT=/tmp/lexradar-first-test/sites/2c83a7d3a1ff2a062531/attempt-0001/dossier
export LR53_PACKET=4b9a72b0b0277e2773c5e6cd3557246fb632535b99f64df35461b9bb0adc1b8a
export LR53_MANIFEST=6b62626ce9f29b8d8277b3b996d3fe0c5a26fc3c22d5e69a22af8a39095a4473
export LR53_RUN=/home/golkonst/src/LexRadar-compact/work/audit-053-doktstom-A
/tmp/lexradar-029-venv/bin/python - <<'PY'
import os
from pathlib import Path
from lexradar.auditors.evidence import load_packet
from lexradar.auditors.preflight import screen
from lexradar.auditors.budget import Budget
from lexradar.single_auditor import profile, messages_for, MAX_INPUT_TOKENS_BOUND
p = load_packet(Path(os.environ['LR53_ROOT']), profile().max_input_bytes)
assert p.sha256 == os.environ['LR53_PACKET']
assert p.manifest_sha256 == os.environ['LR53_MANIFEST']
m, _, _ = messages_for('A', p)
assert sum(len(x['content'].encode()) for x in m) + 1024 <= MAX_INPUT_TOKENS_BOUND
print('packet bytes:', len(p.payload.encode()))
print('reserve USD:', Budget(profile().max_budget_usd).reserve(profile().auditor_a, m))
assert not screen(p), 'STOP: prepare reviewed derivative; new hash/permission required'
PY
```

Только после успешной повторной проверки оператор отдельно даёт разрешение:
«Разрешаю передать OpenRouter пакет SHA256 [точный LR53_PACKET] из [LR53_ROOT]
для одного аудита DeepSeek A, deepseek/deepseek-v3.2, с максимум двумя HTTP
попытками в одном analyze и бюджетом до 0.50 USD». Это текст требуемого
будущего разрешения, а не разрешение, полученное в №53.

Оператор, просмотревший весь packet.payload, самостоятельно создаёт approval
(этот блок здесь НЕ выполнялся):

```bash
# LR53_REVIEWER задаёт реальный согласующий после просмотра материалов.
# LR53_PERMISSION задаётся им только после явного разрешения указанного выше.
export LR53_PERMISSION="${LR53_PACKET}:OpenRouter:DeepSeek-A:USD0.50"
/tmp/lexradar-029-venv/bin/python - <<'PY'
import os, json
from pathlib import Path
from datetime import datetime, UTC
from lexradar.auditors.evidence import load_packet
from lexradar.auditors.preflight import screen, PacketApproval
from lexradar.single_auditor import profile
p = load_packet(Path(os.environ['LR53_ROOT']), profile().max_input_bytes)
assert p.sha256 == os.environ['LR53_PACKET']
assert p.manifest_sha256 == os.environ['LR53_MANIFEST']
assert not screen(p)
assert os.environ['LR53_PERMISSION'] == p.sha256 + ':OpenRouter:DeepSeek-A:USD0.50'
reviewer = os.environ['LR53_REVIEWER'].strip()
assert reviewer
out = Path(os.environ['LR53_RUN'])
out.mkdir(parents=True, exist_ok=False)
a = PacketApproval(packet_sha256=p.sha256, approved=True,
    contents_reviewed=True, reviewer=reviewer, approved_at=datetime.now(UTC))
(out / 'packet.json').write_text(p.payload, encoding='utf-8')
(out / 'approval.json').write_text(a.model_dump_json(indent=2), encoding='utf-8')
(out / 'permission.json').write_text(json.dumps({
    'packet_sha256': p.sha256, 'model': profile().auditor_a.model,
    'max_budget_usd': '0.50', 'scope': 'one analyze; A only',
    'external_transfer': 'OpenRouter', 'reviewer': reviewer,
    'approved_at': a.approved_at.isoformat()}, indent=2), encoding='utf-8')
PY
```

Approval — локальная запись, а не аутентификация согласующего. permission.json
фиксирует бюджет для оператора; analyze читает бюджет из profile(), не из файла.
Ключ следует предоставить защищённым механизмом окружения, без echo, env dump,
аргументов CLI или вставки значения в отчёт. Один запуск:

```bash
/tmp/lexradar-029-venv/bin/python - <<'PY'
import os, json
from pathlib import Path
from lexradar.auditors.evidence import load_packet
from lexradar.auditors.preflight import authorize
from lexradar.auditors.orchestrator import analyze
from lexradar.single_auditor import profile, messages_for, DeepSeekAdapter, commercial
root = Path(os.environ['LR53_ROOT'])
out = Path(os.environ['LR53_RUN'])
p = load_packet(root, profile().max_input_bytes)
assert p.sha256 == os.environ['LR53_PACKET']
assert p.manifest_sha256 == os.environ['LR53_MANIFEST']
assert os.environ['LR53_PERMISSION'] == p.sha256 + ':OpenRouter:DeepSeek-A:USD0.50'
assert not (out / 'analysis').exists(), 'Do not repeat a paid run'
authorize(p, out / 'approval.json')
assert os.environ.get('OPENROUTER_API_KEY'), 'Key must be supplied securely'
report = analyze(root, out / 'analysis', profile(), mode='openrouter',
    allow_external_transfer=True, packet_approval=out / 'approval.json',
    provider=DeepSeekAdapter(), message_builder=messages_for, parallel=False)
fresh = load_packet(root, profile().max_input_bytes)
allowed, reasons = commercial(report, fresh)
(out / 'commercial.json').write_text(json.dumps({
    'outcome': 'HOLD' if reasons else 'GO', 'allowed_findings': allowed,
    'reasons': reasons, 'send_allowed': False, 'release_allowed': False,
    'legal_production_status': 'HOLD'}, ensure_ascii=False, indent=2), encoding='utf-8')
print(report.runs[0].status, 'charged USD:', report.budget_charged_usd)
PY
```

Автоматически команду не повторять даже при ошибке: процесс мог уже отправить
запрос до записи output. Успех inference не означает GO: partial reading снижает
качество доказательства в validator. После результата человек проверяет факты,
нормы, применимость и объём услуги. Генерация КП, отправка и выпуск этими
командами не выполняются.
