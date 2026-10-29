# Задание №54 — восстановление локального досье doktstom.ru

Дата: 16.10.2026. Итог: **NOT READY для передачи Single Auditor DeepSeek A**. Отдельное улучшенное досье подготовлено; воспроизводимость и целостность подтверждены. Политика теперь доступна в текстовом payload. Screening остаётся заблокированным; реального аудита модели не было.

## Границы и сохранность

Сайт, внешние API и сеть не использовались. AI API-вызовов, платных запросов, отправки клиентам, approval, commit, push, merge и deploy не было. Ветка осталась `feat/compact-ai-audit-mvp`, main не изменялся. Collector и действующие модули приложения не редактировались. Добавлены только отдельный скрипт подготовки, синтетические тесты, этот отчёт и локальные артефакты `runs-054/`. В `runs-054/.gitignore` исключены сырые материалы из обычного Git status.

Прочитаны отчёт №53, `collector/provenance.py`, `collector/models.py`, `reading.py`, `text_integrity.py`, `artifacts.py`, `primary/markup.py`, `auditors/evidence.py`, `preflight.py`, `single_auditor.py` и соответствующие тесты. Прочитан skill cloud-environment-runtime; сетевых действий не требовалось. Подход не требует доступов или ключей; секретные файлы не читались.

Снимок SHA256 696 уже существовавших отслеживаемых и неигнорируемых файлов сохранён в `/tmp/lr54-before.json`; окончательная проверка подтвердила их неизменность, включая прежние незакоммиченные изменения. Десять старых manifest также не изменились. Исходное выбранное досье проверено повторно и побайтово сопоставлено с архивной копией. Защищённые файлы не редактировались.

## Почему было только 399 символов

Исходный HTML — 153454 байта, SHA256 `3257dc3e6b801daf640e8dc52c8a219ba393b3a47f8dfdab6c9d47f3a43aa8fa`. Строгий `extract_html` останавливает допуск текста на первом несогласованном закрывающем теге `</span>`: строка 155, столбец 153 (строки от 1, столбцы от 0 в декодированном UTF-8). Причина `Mismatched HTML closing element; suffix withheld`; `extraction_state=partial`, `original_chars=None`, `text_truncated=false`. Число 399 не связано с лимитом токенов, байтов или страниц. `load_packet` воспроизводит именно этот короткий префикс и не использует богатые recovery-результаты compact/primary вместо текста HTML.

Политика находится намного дальше: строки 1017–1043, внутри `<div id="popup__policy">`, вложенного в `class="hidden"`; заголовок на строке 1019. Указатель исходной разметки `data-src="#popup__policy"` расположен на строке 1015. Реальная страница — `https://doktstom.ru/`, ссылка на существующий фрагмент — `https://doktstom.ru/#popup__policy`. Это не отдельная загруженная страница и не PDF. После ошибки на строке 155 строгий парсер до текста политики не допускает. Исполнение popup и видимость в браузере не проверялись.

## Поиск существующих материалов

Проверены доступные `/home/golkonst/src` и `/tmp`, включая игнорируемые runs/work, артефакты collector и результаты primary/compact. Поиск содержимого HTML/JSON по домену дополнен разбором всех найденных `collection.json` и локальных PDF. Исключены `.git`, зависимости, pytest-временные каталоги и новый результат №54. Не удалось читать два системных каталога `/tmp/codex-daemon-1000` и `/tmp/systemd-private-…-systemd-logind.service-…`; глобальная полнота поиска за пределами доступного дерева не утверждается.

Найдено десять прежних досье: в каждом 1 page observation, 0 document observations. У одного старого неуспешного сбора HTML отсутствует; у остальных девяти сохранён побайтово одинаковый HTML с указанным выше SHA256. Дополнительных независимых захватов страниц или политики в этих досье нет.

| Manifest досье | HTML / PDF | SHA256 manifest |
|---|---:|---|
| `/home/golkonst/src/LexRadar-compact/runs-036/doktstom-result/dossier/collection.json` | 1 / 0 | `9d0d456b10c578c4bea06bdfdaab14bfcc759ccca33ecf04b150dec23e49fca0` |
| `/home/golkonst/src/LexRadar-compact/runs-036/doktstom-saved-final/dossier/collection.json` | 1 / 0 | `65dce3454cccf2b05ca3945600f8f3a22df2747936ca69cab920fd808840436d` |
| `/home/golkonst/src/LexRadar-compact/runs-036/doktstom-reviewed/dossier/collection.json` | 1 / 0 | `9d0d456b10c578c4bea06bdfdaab14bfcc759ccca33ecf04b150dec23e49fca0` |
| `/home/golkonst/src/LexRadar-compact/runs-036/doktstom-final/dossier/collection.json` | 1 / 0 | `9d0d456b10c578c4bea06bdfdaab14bfcc759ccca33ecf04b150dec23e49fca0` |
| `/home/golkonst/src/LexRadar-compact/runs-036/doktstom-saved-result/dossier/collection.json` | 1 / 0 | `65dce3454cccf2b05ca3945600f8f3a22df2747936ca69cab920fd808840436d` |
| `/home/golkonst/src/LexRadar-compact/runs-036/doktstom-live-002/dossier/collection.json` | 1 / 0 | `9d0d456b10c578c4bea06bdfdaab14bfcc759ccca33ecf04b150dec23e49fca0` |
| `/home/golkonst/src/LexRadar-compact/runs-036/doktstom-live-001/dossier/collection.json` | 1 / 0 | `dea4ac689313832a42c58f98e45ae2e7bc5aab0f053465321f7802d0f59e7caa` |
| `/home/golkonst/src/LexRadar-compact/work/mvp-doktstom-001/dossier/collection.json` | 1 / 0 | `65dce3454cccf2b05ca3945600f8f3a22df2747936ca69cab920fd808840436d` |
| `/home/golkonst/src/LexRadar/work/smoke-run-001/sites/2c83a7d3a1ff2a062531/attempt-0001/dossier/collection.json` | 0 / 0 | `6e0d4f92361a41540cd71ba79cf0e4dc482ae19afc7214e03016b71592504e8b` |
| `/tmp/lexradar-first-test/sites/2c83a7d3a1ff2a062531/attempt-0001/dossier/collection.json` | 1 / 0 | `6b62626ce9f29b8d8277b3b996d3fe0c5a26fc3c22d5e69a22af8a39095a4473` |

Найдены также локальные производные `runs-036/doktstom-*/analysis/{primary.local.json,trace-map.local.json,compact-analysis.json,packet.json}`, `/tmp/doktstom-primary.json` и `/tmp/lexradar-first-test/sites/2c83a7d3a1ff2a062531/attempt-0001/primary/`. Они полезны для навигации, но не приняты за новые первичные документы и не копировались в evidence как оригиналы. Полный перечень совпадений с доменом текущего поиска: `/tmp/lr54-matches.txt`.

Дополнительно просмотрено извлечение текста 95 локальных PDF-файлов (23 уникальных SHA256): совпадений `doktstom`, `doktorstom`, `доктор стом` не найдено. Один файл `/tmp/lexradar-045-groff.pdf` пуст и не разбирается (`EmptyFileError`). Отсутствие текстового совпадения не исключает сканы без текстового слоя или иные имена. Ни одного PDF с подтверждённой связью с этим доменом не обнаружено; в досье PDF не добавлялись. В исходном HTML нет `.pdf`. Подробная инвентаризация с путями, SHA256, размерами, PDF-страницами и ошибками — `runs-054/doktstom-improved/local-inventory.json`.

## Отдельный импорт и происхождение

Скрипт: `scripts/prepare_doktstom_054.py`. Использованы существующие `SavedMarkup(recover=True)`, `extract_html`, `ArtifactStore`, `MaterialOrigin(kind="manual_html_import")`, `CollectionResult`, `load_packet`, `verify_integrity`, `screen`, `authorize` и `messages_for`. Сборщик не перерабатывался. Скрипт запрещает DNS и socket connect при CLI-запуске, не создаёт провайдера, не обезличивает и не выдаёт approval. При существующем output отказывается перезаписывать его.

В новой копии выполнены ровно пять правок, соответствующих dispositions recovery-парсера; изменения применены справа налево по координатам исходника:

| Строка : столбец | Исходный токен | Производный токен |
|---|---|---|
| 155 : 153 | `</span>` | удалён stray close |
| 179 : 158 | `</span>` | удалён stray close |
| 347 : 94 | `<div class"unit-title unit-title--h2">` | `<div>` (двусмысленные атрибуты отброшены) |
| 413 : 228 | `</p>` | удалён stray close |
| 413 : 1607 | `</li>` | удалён stray close |

Ни один извлечённый recovery-текст не изменён: строгий текст производного HTML **точно равен** `compact(SavedMarkup.parts)` оригинала — 15227 символов. Неподдерживаемые diagnostics, quarantined suffix, ошибочная UTF-8 или несовпадение текста приводят к отказу. JS/script/style и прочие исключаемые блоки не попадают в текст модели.

`source_sha256` контракта manual_html_import указывает на HTML, непосредственно импортированный как оригинал нового пакета, то есть на явно производный HTML. SHA256 настоящего исходного захвата сохранён отдельно в origin.limitations и provenance. Это необходимо: `load_packet` требует совпадения `source_sha256` с HTML-артефактом текущего досье. Производный файл не выдаётся за HTTP capture: `http_status=None`, `http_response_at=None`, роли времени `local_import`, `origin_authenticated=false`, `original_capture_at` сохранён из выбранной страницы. Начало импорта `2026-10-16T09:03:45.429942Z`. Исходная дата страницы — локальное наблюдение `2026-10-13T15:08:32.556600Z`, а не аутентифицированное время ответа издателя.

У производного HTML `reading.extraction_state=complete`, `provenance=reproduced`: это полнота извлечения только исправленного файла. Исходный recovery остаётся **partial**; это явно указано в origin обеих сущностей, warnings, observed_fact и `provenance.json` с исходными diagnostic/text spans. Evidence нового пакета имеет status=partial из-за recovery и отсутствующего нового скриншота. Старые screenshots сохранены в архиве оригинала, не прикреплены к исправленному HTML. Единственная прежняя форма сохранена как старое наблюдение, с unavailable_reason нового скриншота; остальные формы/страницы не выдумывались.

## Политика, цитаты и доступность аудитору

Во входе A теперь есть заголовок политики, термины и разделы 1–7: защита, получение и использование данных, коммуникация, безопасность, получение информации/отзыв согласия и изменения. Политика — один встроенный текстовый документ внутри одной HTML-страницы, а не новый `DocumentObservation` (эта схема предназначена для PDF). Всего: **1 страница, 0 PDF-документов, 1 встроенная политика, 1 сохранённое наблюдение формы**.

Самостоятельный текстовый excerpt политики — 3232 символа, `policy-excerpt.txt`, SHA256 `834e05814e2ff0610fc533a5efd12e471cd1abdbc2b67a61c51a790023186594`. В payload он входит как непрерывная подстрока page.text. `quotations.json` содержит пять дословных цитат, реальные URL/fragment reference, evidence_id, исходные строки и смещения, смещения в page.text, SHA256 цитат и первичного HTML. Проверено присутствие каждой цитаты в оригинале и в воспроизведённом тексте. Примеры:

> «Оставляя свои персональные данные на сайте, Вы соглашаетесь с настоящей Политикой конфиденциальности.» — строка 1020.

> «Сайт обеспечивает безопасность персональных данных от несанкционированного использования их третьими лицами.» — строка 1037.

Эти локальные цитаты не являются правовым заключением. Их достаточность и актуальность требуют отдельного чтения. Скрытый HTML не доказывает показ пользователю или валидное согласие.

`messages_for("A", packet)` включает именно packet.payload в user message; цитаты и вся политика проверены в этом тексте. HTML, PDF binaries и пиксели аудитору не отправляются. Проверены повторный load_packet, отсутствие mismatch, SHA256 всех артефактов, лимиты и повторная сборка с тем же imported_at: manifest и payload получились побайтово идентичными.

| Метрика | Исходное | Улучшенное |
|---|---:|---:|
| HTML-страниц | 1 | 1 |
| PDF-документов | 0 | 0 |
| Извлечённых символов page.text | 399 | 15227 |
| Фактический payload, UTF-8 байт | 7139 | 34319 |
| Срабатываний screening | 4 | 6 |

Фактический текст сообщений system+user — 45452 байта; консервативная граница с framing 1024 — 46476, меньше 240000 у Single A. Payload меньше 200000 байт профиля. Локальный Budget.reserve — $0.054476 на попытку, две попытки — $0.108952, меньше $0.50. Расчёт локальный; проверка тарифов/доступности модели/баланса не выполнялась.

## Пути и хеши

Все относительные пути далее от `/home/golkonst/src/LexRadar-compact`. Исходное досье по №53: `/tmp/lexradar-first-test/sites/2c83a7d3a1ff2a062531/attempt-0001/dossier`. Его полная побайтовая архивная копия: `runs-054/doktstom-improved/original-dossier`. Улучшенное: `runs-054/doktstom-improved/dossier`. Снимок точного исходящего текста: `runs-054/doktstom-improved/packet.json`.

| Объект | SHA256 |
|---|---|
| Исходный collection.json | `6b62626ce9f29b8d8277b3b996d3fe0c5a26fc3c22d5e69a22af8a39095a4473` |
| Исходный payload | `4b9a72b0b0277e2773c5e6cd3557246fb632535b99f64df35461b9bb0adc1b8a` |
| Улучшенный collection.json | `0e7f728a9f130d4f7e46b8c0e0ca30cefb18d71734b6bfb12468b26bb55d46b0` |
| Улучшенный payload | `42d33ebbf0e329b6ea76bb04bdcb9f844877a8f046c18610ce378ca8ab7f06f9` |
| Производный HTML | `7e079cc0618b4431aa9bf6c543309ad48530435eab298409f3beed34b35ace11` |
| Производный text | `5db5316523d5800e07265c13ce655792a0ab749d7d0db04fe8b9de87e491bab0` |
| original-SHA256SUMS.txt | `fe1dc4ea5cca3c8099ca8f518a78f940d066d066713927395fa5937f7ba170d7` |
| improved-SHA256SUMS.txt | `37218016c12c271ddbbf5ba4b8216136637ba1b55f69e72839c8f38a6c94cfcd` |

Последние две строки — SHA256 сортированного списка `SHA256  relative/path
` всех файлов соответствующего досье (включая crawl оригинала); это воспроизводимый файловый отпечаток дерева, не хеш tar. Полные пофайловые хеши находятся в этих двух списках и `provenance.json`. SHA manifest, SHA дерева и SHA payload имеют разный смысл.

Дополнительные сохранённые результаты в `runs-054/doktstom-improved/`:

| Файл | SHA256 |
|---|---|
| `provenance.json` | `e932a4f298c3efd0bb3318039c737ef98c4fb21b6e414a22db5ec855594c6163` |
| `validation.json` | `2f782890154b7d9db7149f142c1f94962a6ec20745781bab82efcc097cee4bb2` |
| `final-check.json` | `aff40e0b633844539046c5b5e735d736bcf49a9502a91d5add64fadf2d8df0b2` |
| `local-inventory.json` | `830c33872583da9db5b7857d5216094ee29b5d939ee2a2113c901fc6b3ba0f7a` |
| `quotations.json` | `508c8e61166c8d04148f62f5deb7961dbcea370650c30cd89a6a800d48c0f119` |
| `policy-excerpt.txt` | `834e05814e2ff0610fc533a5efd12e471cd1abdbc2b67a61c51a790023186594` |

## Screening и точный оставшийся блокер

`verify_integrity` возвращает `[]`, но screening не проходит. В `validation.json` сохранены категории и JSON paths без публикации совпавших персональных значений:

| Путь | Категории |
|---|---|
| `$.untrusted_collector_data.pages[0].forms[0].submission_context` | birth_date |
| `$.untrusted_collector_data.pages[0].text` | email, phone_or_identifier, identity_or_health_context, person_name, birth_date |

Это шесть срабатываний по пяти категориям. В контексте формы дата `27.07.2006` относится к Федеральному закону, но эвристика классифицирует любую допустимую календарную дату как birth_date. В page.text встречаются также `01.12.2020`, `25.12.2020`, `27.07.2006`; медицинская лексика, публичные контакты и имена также срабатывают по общим шаблонам. Это не установленное наличие медицинской тайны, однако автоматическое разрешение не имеет исключения для публичных реквизитов и дат нормативных актов.

`authorize(packet, None)` возвращает **`Potential sensitive data: transfer blocked; review/redact and repeat`**. Проверка screening выполняется раньше approval; согласие на этот SHA само по себе не снимет блок. Функции, шаблоны screening и исходящие байты не подменялись; тестовый провайдер не использовался для имитации реального аудита.

Для продолжения нужен человеческий просмотр полного payload и всех метаданных, затем отдельный документированный производный импорт с адресной обезличивающей редакцией (включая submission_context, имена/контакты, медицинский контекст и даты, если они остаются блокерами). Нужно сохранить оригиналы и точные цитаты локально, журнал диапазонов замен и ограничения утраты доказательств; нельзя выдавать изменённые цитаты за дословные. Удалять все триггерные слова вслепую нельзя: это может уничтожить юридически важный смысл политики. Альтернатива требует отдельно согласованного изменения политики screening/контракта, которого в №54 нет. После любой редакции необходимы новый load_packet, screen без срабатываний, новые хеши, проверка юридической пригодности и человеческое approval точных байтов; затем отдельное явное разрешение на конкретный API/модель/бюджет. Текущее задание не разрешает внешний запуск.

## Офлайн-тесты и воспроизведение

Добавлены `tests/test_prepare_doktstom_054.py`: точные правки и сохранение текста политики, исключение script-текста, отказ на неоднозначной структуре/неполном raw-text/повреждённом UTF-8, roundtrip manual_html_import, отсутствие HTTP-метаданных, неизменность исходника и архива, передача текста в messages_for A, сохранение screening-блокировки, запрет перезаписи, отказ на порче артефакта и вложенном output. Все сетевые и AI transport входы в новых тестах блокируются.

Команда выполнена локально (провайдер не создавался):

```bash
PYTHONPATH=src /tmp/lexradar-029-venv/bin/pytest -q \
  tests/test_prepare_doktstom_054.py tests/test_resilient_html.py \
  tests/test_reading_integrity.py tests/test_preflight_dates.py \
  tests/test_single_auditor_050.py
```

Результат: **130 passed in 26.47s** (`runs-054/tests.log`). Ruff check и format --check для двух новых Python-файлов прошли (`runs-054/ruff.log`). До расширенного набора также прошли 93 теста за 24.39s. Полный suite не запускался: старые изменения вне задания оставлены нетронутыми.

Подготовка нового отдельного импорта (выбрать ещё не существующий output; команда не авторизует передачу):

```bash
PYTHONPATH=src /tmp/lexradar-029-venv/bin/python scripts/prepare_doktstom_054.py \
  runs-054/doktstom-improved/original-dossier /tmp/doktstom-054-new-import
```

Время нового импорта по умолчанию новое, поэтому его SHA manifest/payload изменится. Для проверки побайтовой воспроизводимости использован `prepare(..., imported_at=datetime.fromisoformat("2026-10-16T09:03:45.429942Z"))` в отдельном временном каталоге: оба SHA совпали. Подробные диагностические скрипты сохранены в `runs-054/inventory_local.py` и `runs-054/verify_local.py`; последний использует снимок `/tmp/lr54-before.json` этой сессии для контроля прежних файлов.

**Оценка: NOT READY.** Материалы существенно улучшены и политика технически доступна A, но screening запрещает передачу; юридическая полнота исходника, актуальность сайта и внешнее разрешение не установлены. Реального ответа DeepSeek A и результата юридического аудита в задании №54 нет.
