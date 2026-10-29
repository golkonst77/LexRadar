# HTML-разбор aclinic-krd.ru: диагностика без ослабления доказательств

Проверено 29.10.2026 на сохранённом досье
`/home/golkonst/lexradar-data/parser-c8e0008cfad0538c9b5703f7c9e5ea76845e52d582788c06942d8c3ab3368b9f/audit/sites/row-000002/dossier`.

## Причина

На всех трёх страницах первый `StaticText.uncertain()` вызывается на
`<feColorMatrix ... />` внутри `<svg><defs><filter>` WordPress duotone:
page-0001 — строка 204, столбец 236; page-0002 — 196:236; page-0003 — 197:236.
HTMLParser приводит имя к `fecolormatrix`, которого нет в SVG-списке
`handle_startendtag`. Тег попадает в стек незакрытых элементов и включается
постоянный карантин. Последующие закрытия filter/defs/svg не соответствуют вершине
стека. До ошибки есть только пробелы: после нормализации строгий текст пуст.
`main_only=False/True` даёт одинаковый результат; main на этих страницах не найден.
Это не потеря при записи txt и не ошибка декодирования.

## Изменение и границы

Изменены только:

- `src/lexradar/collector/reading.py`: дополнительный
  `reading.diagnostic_html` с текстом, флагом ограничения длины, фиксированными
  `extraction_state=partial`, `evidence_eligible=false` и предупреждением.
  Диагностический парсер допускает дисбаланс обычных контейнеров; наследует
  исключение script/style/template/head/title/noscript, проверку некорректных
  закрывающих токенов и карантин неоднозначных исключённых областей.
- `src/lexradar/auditors/evidence.py`: диагностическое поле исключено из AI payload.
- `tests/test_html_diagnostics.py`: регрессии и интеграционные проверки.
- Этот отчёт.

Строгий алгоритм и существующий `recover_html` не менялись. Диагностика охватывает
весь сохранённый HTML, не претендует на полноту или браузерную видимость, не
исполняет HTML/JS, не извлекает значения атрибутов и не является каналом цитат.
Она сохраняется в collection.json нового сбора; при load_packet заново вычисляется
в памяти из проверенного оригинала, а не принимается на веру из манифеста.
Отдельный txt-артефакт не добавлен, строгий page-*.txt не подменяется.

| Артефакт | HTML, байт | Строгий текст, символов | Диагностический текст, символов |
|---|---:|---:|---:|
| page-0001 | 125716 | 0 | 2480 |
| page-0002 | 102766 | 0 | 3217 |
| page-0003 | 103193 | 0 | 1830 |

Для всех трёх `extraction_state=partial`, `original_chars=null`,
`text_coverage_complete=false`. В реальном досье `provenance=reproduced` означает
воспроизводимость строгого производного текста, а не его полноту. Все пять policy5
остались UNDEFINED/confirmed=false. quote_locations не принимает диагностический
текст. verify_integrity вернул пустой список; SHA-256 манифеста и всех восьми
артефактов до/после совпали. Старые review/audit не переписывались.

policy5 по-прежнему читает только material.text и требует воспроизводимый полный
читаемый текст. evidence-review использует checked_report/commercial: GO зависит
от допустимых подтверждённых выводов; диагностический текст их не создаёт.
В mvp_batch prepare отсутствие подтверждённых триггеров означает
SKIPPED/HOLD (`no_confirmed_triggers_1_2_3_5`), а отсутствие доступных артефактов —
отдельный SKIPPED/HOLD. Непустая диагностика сама по себе не меняет эти решения.

## Проверки

Целевой набор: test_html_diagnostics, test_static_text, test_policy_recovery,
test_reading_integrity, test_policy5, test_legal_documents,
test_collection_readiness: **205 passed in 25.81s**, включая 20 новых проверок.
Проверяются ранний self-closing, несовпадающий конец,
незакрытые теги, валидный HTML, исключённые области, ограничение длины, отсутствие
диагностики в AI payload, неподтверждение policy5/правовых правил, целостность,
воспроизведение из оригинала и новый статический сбор без сокетов.

Расширенный набор (collector, crawl_policy, auditors, primary,
policy_review_checks, fast_checks_2_3, offline_mvp_048): 264 passed, 27 failed,
102 errors. В копии исходного рабочего дерева до правок: 263 passed, 28 failed,
102 errors; все падения после изменения уже присутствуют до него. 102 ошибки —
запрет создания сокетов песочницей. Остальные падения — существующие проверки
primary (`Dossier changed during primary inspection`) и crawl options.
test_operator_audit_102 не собрался: отсутствует импортируемый mask_fields в
test_approved_proposal_084. Эти чужие изменения/тесты не исправлялись.
Ruff для reading.py и нового теста проходит; evidence.py имеет прежний I001
в блоке локальных импортов, этот блок не менялся.

## Одна команда нового сбора на сервере

Из корня проекта, с существующим окружением LexRadar. Создаёт уникальный каталог,
получает до трёх HTML и двух документов только этой клиники в статическом режиме.
Никаких AI, CRM, почты или подготовки КП. Сеть и robots.txt могут ограничить сбор.
Каталог выводится в терминал; диагностический текст — в
`dossier/collection.json → pages[].reading.diagnostic_html.text`.

```bash
PYTHONPATH=src /home/golkonst/src/LexRadar/.venv/bin/python - <<'PY'
from pathlib import Path
from tempfile import mkdtemp
from lexradar.collector import CrawlOptions, Limits, collect
from lexradar.auditors.evidence import load_packet

root = Path(mkdtemp(prefix="aclinic-html-recheck-", dir="/home/golkonst/lexradar-data"))
collect("https://aclinic-krd.ru/", root / "dossier",
        Limits(max_pages=3, max_documents=2), static_only=True,
        crawl_options=CrawlOptions(max_requests=16, max_duration_seconds=120))
packet = load_packet(root / "dossier", 20_000_000)
print(root)
for page in packet.data.pages:
    diagnostic = page.reading.diagnostic_html
    print(page.evidence_id, page.final_url or page.requested_url,
          "strict_chars=", len(page.text or ""),
          "diagnostic_chars=", len(diagnostic.text) if diagnostic else 0,
          "state=", page.reading.extraction_state,
          "coverage_complete=", page.reading.text_coverage_complete)
PY
```

Свежий сетевой сбор здесь не запускался. Юридически допустимый непустой текст
этой клиники не восстановлен: для новых выводов нужна отдельная проверка источника.
Добавленное поле — расширение JSON-контракта; старые версии с extra=forbid могут
не прочитать новый collection.json, для повторной проверки использовать этот код.
