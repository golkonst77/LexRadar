# Нормативная матрица PR #15

```bash
lexradar verify-norms examples/primary-audit/dossier \
  --primary examples/primary-audit/result/audit.json \
  --assessment examples/primary-audit/result/internal-assessment.json \
  --registry examples/legal-sources/registry.json \
  --event-date 2026-10-10 --as-of 2026-10-10T00:00:00Z \
  --output work/normative
```

Режим полностью офлайн. Новый каталог содержит `norm-requirements.json`,
`requirement-applicability.json`, `normative-verification-gaps.json`. Старые 15
первичных и четыре независимых экспорта не перезаписываются. Для воспроизводимости
нужно явно задавать дату события и timestamp оценки. Все результаты внутренние,
HOLD/not_confirmed, без GO, клиентского выпуска, штрафов и отправки.

## Источники и требования

Расширен существующий `SourceRegistry` двумя необязательными массивами:
`requirements` (аннотации положений) и `source_intakes` (заявленные сведения о ручном
получении/рассмотрении). Старый реестр без них загружается. Источник, текст, URL,
редакция, пункт и интервалы сохраняются в существующей `LegalSourceCard`.
`NormRequirementSpec` ссылается на ID карточки, содержит категории, условия,
необходимые доказательства, исключения и альтернативы. Второго хранилища НПА нет.

`NormRequirement` — результат проверки конкретного положения: точная цитата,
ожидаемый и фактический SHA-256, дата события, заявленные интервалы, первоисточник,
условия, ограничения и независимые статусы. `matched_saved_text` означает лишь
совпадение цитаты с безопасно прочитанными локальными UTF-8 байтами. Это не проверка
официального происхождения или юридической силы. Поля дат не аутентифицированы:
`expired` / `not_yet_effective` / `within_declared_interval` относятся к **заявленному**
интервалу, а не к доказанному состоянию закона. Отдельно отмечаются отсутствующий,
неполный, устаревший и противоречивый источник. Пересекающиеся редакции не разрешаются
выбором самой новой даты. Отсутствие строки с номером закона в политике не исследуется
как самостоятельный дефект.

Карточки без аннотаций сохраняются как кандидаты по области, с неизвестными
юридическими условиями. Это подбор направления, а не автоматическая применимость
к каждому вопросу категории. Все аннотации JSON остаются `untrusted_requires_legal_review`.

Если реальных текстов нет, результат содержит задачи поиска: 152-ФЗ, 149-ФЗ,
38-ФЗ, 323-ФЗ, 2300-1, действующие правила платных медицинских услуг, подзаконные
требования к политике/медицинскому раскрытию, нормы ответственности КоАП РФ.
Это направления из задания. В задачах **нет выдуманных статей, цитат, редакций,
дат или штрафов**: эксперт должен определить точное положение по первоисточнику.
Наличие даже подходящей карточки не закрывает такую задачу автоматически.
Поставляемый пример реестра — исключительно синтетические тексты, не нормы РФ.

## Условия применимости и отсутствие сведений

Перед оценкой выполняется существующая независимая сверка PR #14. Импортированный
`VerificationReport`, confidence и human-флаги не принимаются как доверенное основание.
Матрица отдельно показывает восемь аспектов: действие нормы на дату, локальную
проверку текста, редакцию, деятельность, обязанное лицо, фактическое действие,
исключения и достаточность доказательств. Локальный текст может совпасть, когда
все остальные аспекты остаются неизвестными.

Условия имеют ограниченный набор проверяемых предикатов. Статическое наличие формы,
упоминания организации или декларации ресурса может иметь `supported_static_fact`
с ID фактов/материалов. Нельзя повысить его до фактической обработки, рекламы,
передачи Medflex или cookies GTM. Название организации не устанавливает оператора;
слово «врач» и sector hint не устанавливают медицинского исполнителя. Роли разных
организаций сохраняются отдельно. Наличие фрагмента политики не означает чтение
полного действующего документа. Исключения и альтернативы не считаются проверенными
на основании списка в JSON.

Контракт содержит все запрошенные статусы, но отсутствие аутентифицированного
происхождения удерживает текущий результат в `source_unverified`; отдельно
фиксируется `revision_unverified` и поддержка ограниченных технических условий.
`conditions_supported` и `not_applicable_with_evidence` зарезервированы и отвергаются
валидатором MVP: подтверждать юридический контекст и правовое исключение сейчас
некому. Это ограничение доверия, а не симуляция юридической экспертизы.

`AbsenceCheck` указывает область сохранённых материалов, полноту их текста и
непрочитанные известные ссылки (либо unknown). Знаменатель соответствующих разделов
сайта и фактический исполнитель неизвестны, поэтому отсутствие обязательных сведений
не доказано даже при полностью прочитанном одном HTML. Скан, непрочитанный документ,
неполная страница или отсутствие checkbox не становятся нарушением.

## Коммерческие кандидаты

Отдельно представлены наличие нормативного требования, применимость, недостаточность
существующего документа, новый документ, корректировка и чисто техническое изменение.
Ни один из них не подтверждается автоматически. Условная внутренняя проверка объёма
допустима только при воспроизведённом коммерческом основании и доступном совпавшем
тексте кандидата-нормы; она не означает рекомендацию продать документ.
При отсутствии нормативного основания — `insufficient_normative_basis`.
Согласие не считается обязательным основанием любой обработки. Медицинские документы
не предлагаются как необходимые без установления исполнителя и применимых обязанностей.

## Первая ручная проверка

1. Найти официальные публикации и точные положения для конкретного вопроса.
2. Сохранить текст и происхождение, проверить редакцию, изменения и переходные правила
   на явно заданную дату. Сеть в этой команде отсутствует; загрузку выполняет человек.
3. Подготовить существующую карточку через `lexradar legal-source prepare --metadata ...
   --text ... --event-date ... --output ...`. Проверить точную цитату и хешы.
4. В реестре связать аннотацию требования с ID карточки; проверить, что условия,
   исключения и альтернативные основания следуют из конкретного положения.
5. Для `source_intakes` указать ID карточки, `original_sha256`, `card_sha256`,
   `acquired_at`, `reviewer`, `reviewed_at`. URL и редакция берутся из карточки и
   привязаны её хешем. Несовпадение хешей или неверный порядок дат аннулирует запись.
6. Использовать существующий `legal-source review` для журнала рассмотрения.
   Эти записи всегда untrusted: редактирование JSON не создаёт аутентификацию.
7. Установить исполнителя/оператора, деятельность и реальные процессы; сначала
   прочитать доступные документы полностью, затем определить действительно необходимые
   дополнительные сведения, проверить опровержения, исключения и альтернативы.

MVP поддерживает только сохранённый UTF-8 нормативный текст; не получает официальный
PDF, не выполняет OCR и не удостоверяет происхождение извлечённого из PDF текста.
Техническое завершение не означает завершённости нормативной/юридической проверки.
Актуальность законодательства на октябрь 2026 года не заявляется. Доверенный human
workflow и положительный production допуск требуют отдельной будущей реализации.
# Saved policy observations and research context

`verify-norms` independently inspects the original saved HTML for bounded embedded
policy candidates. `saved_policy_observations` includes the evidence ID, source URL,
original artifact hash/path, decoded-source coordinates, extracted text/hash,
local reading completeness and separate page completeness. The legacy predicate
`policy_text_available` and explicit `saved_policy_text_present` mean **saved text
presence only**. They do not mean the policy applies to a particular form/operator.
`live_policy_accessibility` remains unknown offline. Hidden popup contents may be
read completely even when a different page region is damaged; that never upgrades
the page or establishes currentness, public accessibility or legal sufficiency.
Script, style, template, head, title and noscript contents are excluded. Detection
is lexical (policy heading/identifier plus substantial personal-data text), not
legal classification. Unidentified layouts can be missed; no absence is inferred.
Complete nested documents take precedence over partial/banner wrappers; identical
text in disjoint blocks retains distinct source ranges. The previous independently
reproduced PDF-text path remains available, without claiming complete visual review.

An optional `SourceRegistry.research_contexts` list preserves imported research
without establishing production trust. Each entry binds to the exact card hash,
provision, declared revision and control date, with local materials (relative path,
SHA-256, role), scope, claimed conclusion and limitations. `provenance_method` may
be `user_attested`; `edition_basis` may be `secondary_SPS_consolidation`. Those are
explicit **imported declarations**, not an independently authenticated official
source or an editorial-history check performed by the program.

The output separates `imported_claim` from `mechanism_operations`: the latter lists
only the byte/hash and metadata comparisons actually performed. Missing, changed,
unsafe or misbound research materials invalidate the context binding while keeping
the original claim for diagnosis. Evidence is checked again before export. Even a
context whose sidecars match is invalidated if the original normative text/quote is
missing, changed or inconsistent. A consistent binding remains
`production_trust=untrusted`, with official-portal
verification and claim authentication false. Editable research conclusions cannot
change `revision_check`, establish a violation, produce GO or release client text.
An omitted context remains compatible with earlier registries and outputs.
