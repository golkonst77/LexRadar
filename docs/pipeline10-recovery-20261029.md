# Сопоставление pipeline10 с прежним механизмом, 29.10.2026

## Что подтверждено

Исследованы текущие pipeline10, mvp_batch, proposal_rules, collector/robots,
operator_audit, тесты prepare/express, документация и сохранённые manifest/collection,
HTML, robots-response и PDF. Посторонние изменения checkout не откатывались.
pipeline10 и значительная часть вчерашних доработок не отслеживаются git;
последний кодовый checkpoint b3a8cd5 датирован 16.10, последующие 5ee4f0f и
3cee963 от 29.10 меняют документацию. В git нет отдельного вчерашнего
проверенного релиза с обещанными 50%. Поэтому источником фактических настроек
служат сохранённые manifest.json, а не предположение по дате коммита.

| Путь | Что фактически делает |
|---|---|
| mvp_batch prepare | Бесплатный статический сбор, evaluate_rules, render_client, write_html_pdf; в текущих сохранённых прогонах express-screening-144 |
| pipeline10 без analysis | Вызывает именно этот prepare, max_sites=1, max_ai_calls=0, штатный resume |
| mvp_batch saved | Реальный записанный Single A + привязанное досье + независимый legal_review, qualify; иной допуск к production |
| lexradar audit | Операторский сбор до 40 страниц/20 документов/100 запросов/600 секунд; по умолчанию только подготовка пакета; AI требует отдельного разрешения |
| operator audit --saved-audit | Воспроизводит original.txt и коммерческую оценку сохранённого AI; не заменяет fixed-rule prepare |

Сохранённые `runs/production-2026-10-27-medical-30{,-rerun}/manifest.json`
и `runs/experiment-ignore-robots-2026-10-28-fixed-https/manifest.json`
содержат mode=prepare, express-screening-144, max_ai_calls=0. Последний содержит
**ignore_robots=true**; это эксперимент, не допустимая настройка нового конвейера.
Совпадающих доменов с текущими десятью в найденных runs/*/manifest.json нет.
Воспроизвести 50% на той же выборке по этим журналам невозможно.
Проверяемый ранее интерфейс: `python -m lexradar.mvp_batch CSV --mode prepare
--allow-site-collection --max-ai-calls 0 --max-sites N --output DIR`.
Политики, пороги, тексты и шаблон в этой задаче не менялись.

## Те же десять организаций

Источник: `/home/golkonst/lexradar-pipeline10-20261029/report.json` и
`<parser-id>/prepare/sites/row-000002/dossier/`.
Все десять досье загружены через load_packet с проверкой целостности;
evaluate_rules вызван заново без сети и без AI. Результат совпал с прежним:
два триггера 1, остальные без триггеров. Оба существующих PDF прошли pdf_check.

| Сайт | Получено HTML / попыток; полных текстов | Причина и решение на сохранённом материале |
|---|---|---|
| aclinic-krd.ru | 6/6; 6 | Триггер 1, полный шаблон КП/PDF; REVIEW до содержательного одобрения |
| unionclinic.ru | 2/2; 1 | Главная: конфликт закрывающих HTML-тегов, удержан суффикс; триггеров нет, HOLD по недостаточным основаниям |
| plastica.onclinic.ru | 4/4; 0 | Конфликты разметки/декодирования; дополнительно File size limit exceeded; HOLD, отсутствие недостатков не доказано |
| www.onclinic.ru | 0/1; 0 | robots parser отвергает User-agent: Yahoo!-MMCrawler/3.x (строка 441), также есть другие недопустимые агенты; HOLD до получения применимой однозначной политики |
| www.onclinic-ryazan.ru | 0/1; 0 | robots: Disallow: &search_source в применимой группе (строка 279; повторения 448/617), Invalid robots path; HOLD |
| aclinic.ru | 2/4; 2 | Политика и согласие заблокированы robots_disallow; триггеров нет, HOLD; запрет не обходился |
| baby.onclinic.ru | 6/7; 5 | Триггер 1, КП/PDF; одна страница блокирована cross-origin, главная частична; REVIEW |
| dent-home.ru | 5/5; 0 | Конфликт закрывающих HTML-тегов, лишь 91–164 символа на страницу; HOLD по недостаточным основаниям |
| estetic-spb.ru | 0/1; 0 | Cross-origin navigation blocked при получении robots; HOLD, иной origin автоматически не разрешён |
| amedaclinic.ru | 5/5; 0 | Конфликты/незакрытые HTML-теги, тексты по 429 символов; HOLD по недостаточным основаниям |

Ошибки robots приходят из общего collector, не из подмены agent/path в pipeline10.
Это ограничение прежнего строгого парсера и same-origin, а не доказанный новый
дефект интеграции. Его грамматика/запреты здесь не ослаблены. Нет оснований
объявить пять остальных HOLD правовым соответствием сайта или придумать им нарушения.

## Исправленная связка

- Убрана обязательная доступность CRM перед локальным аудитом. Явный check-crm
  остаётся; сбой CRM сохраняется отдельно, а запись по-прежнему блокируется.
- Email больше не теряется во входном CSV аудита. Неподтверждённое имя по-прежнему
  не используется для персонализации: это сохранённое условие identity gate.
- Убрано ошибочное сообщение «Technical draft only»: используется тот же полный
  утверждённый шаблон КП. REVIEW и требование проверки постоянного блока 4
  сохранены; техническая готовность PDF не подменяет production/legal approval.
- Добавлены детали страниц, ограничений чтения, ошибок сбора, триггеров,
  отдельные audit_success/proposal_eligible/full_PDF/production_qualified.
  Они сохраняются даже при HOLD и ошибке PDF; штатный heartbeat работает.
- Экспортёр, журнал, schema API, legal_review, точный SHA PDF, identity/export
  approval и confirm-create-drafts не изменены. Реальных CRM-вызовов не было.

## Проверки и воспроизведение

94 теста + 46 subtests: pipeline10, prepare_proposal_fix, parser_import,
espo_export, mvp_batch_progress. Полный сетевой crawl_policy/express-набор
пытался открыть localhost и заблокирован sandbox (PermissionError errno=1);
его нельзя считать пройденным. Отдельно прошли 14 чистых robots-тестов без сокетов.

Офлайн-прогон оркестратора с подстановкой сохранённых результатов mvp_batch
и повторным настоящим evaluate_rules/pdf_check: 10 processed, audit_success=7,
proposal_eligible=2, full_PDF=2, REVIEW=2, HOLD=8, CRM_Drafts=0. Это replay,
не новый сбор или генерация PDF. Результаты: `/tmp/lexradar-pipeline10-saved-replay-20261029`.

CSV доступен. Пробный новый сбор в sandbox: 10 HOLD / DNS resolution failed,
0 PDF, 0 CRM Draft; `/tmp/lexradar-pipeline10-repair-20261029`.
Parser v3 не запускался. Единственная команда для обычного терминала:

```bash
cd /home/golkonst/src/LexRadar-compact && PYTHONPATH=src /home/golkonst/src/LexRadar/.venv/bin/python -u -m lexradar.pipeline10 /home/golkonst/lexradar-batch10-20261029-g3YHX9/clinics.csv --allow-site-collection
```

Новый приватный output выбирается автоматически. Без флагов CRM нет даже
обращений к CRM. Ожидаемая семантика: REVIEW при наличии КП, HOLD при запрете
или недостаточных основаниях, ERROR при отдельной ошибке, продолжение пакета;
CRM_Drafts=0 гарантируется отсутствием confirm. Числа 2/8 относятся к сохранённым
досье, не прогноз свежей сети. 50% пригодных организаций не гарантируются.
