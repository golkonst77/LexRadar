# Архитектура и эксплуатационные риски — 04.11.2026

Проверка исходников и локальных снимков, без запуска системы. HEAD web-control — 5782753, compact — d351e81; оба дерева имеют чужие незакоммиченные изменения. Удалённые ветки не проверялись. Лимит — 2 часа по AGENTS.md.

## Карта

| Компонент | Источник / зависимость | Состояние и граница |
|---|---|---|
| Control Center | control_mvp.py:846 main; control_center.py и control_pages.py | ThreadingHTTPServer, localhost:8080; основной четырёхшаговый UI только в MVP |
| Организации | control_catalog.py:70 Catalog.snapshot; control_leads.py:82 load_leads | /home/golkonst/lexradar-data и сохранённые parser.csv; ограниченный индекс, объединение по домену |
| Сбор | control_parser.py:18 Parser; control_parser_runner.py | Внешний Parser-v3, только после подтверждения; в этой ревизии не запускался |
| Очередь анализа | control_jobs.py:45 Jobs; control_analysis.py:45 analysis_history | .control-center-state/jobs.sqlite3, worker.lock, один поток, транзакции постановки |
| Движок | control_analysis.py:12 ENGINE; control_analysis_runner.py:59 main | /home/golkonst/src/LexRadar-compact/src, Python из LexRadar/.venv; один сайт, без платных вызовов |
| Коммерческий итог | control_enrichment.py:36 commercial_decision, :75 observation | results.csv + review.json; квалификация и юридический допуск различаются; код не менялся |
| Текущее КП | control_offers.py:21 publish, :143 verified | SQLite-указатель на домен, PDF/HTML SHA-256; поздний HOLD сохраняет старое КП |
| CRM preview/confirm | control_mvp.py crm_selection_preview, do_POST; src/lexradar/crm_prepare.py и espo_export.py | GET локальный; подтверждённый POST последовательно Account→PDF/Draft, постоянные journals, binding/intent/HOLD |
| Планирование | control_mailing.py:182 cycle, :304 start | Поток в main каждые 60 секунд; mailing.lock + прежний .mailer.lock; записи CRM возможны при штатном старте |
| Старый mailer | /home/golkonst/lexradar-mailer/mailer_v2.py:226 schedule | Отдельные batch/config/approved/events; не запускался, не изменялся |
| Сделки | lexradar-mailer/lexradar_deals.py:54 prepare, :93 sync | Старый watcher меняет стадии; новая очередь сохраняет собственные intent/ID |
| Контроль продаж | control_followup.py:103 read_sent, :192 Followup | GET CRM, локальный snapshot/actions/events/blocked в followup.sqlite3; GET страницы обновляет локальный снимок |

Read-only страницы не эквивалентны read-only запуску приложения. Jobs.__init__ меняет статусы, вызывает publish/cleanup; main запускает mailing. Поэтому для исследования использовался объект без конструктора, SQLite mode=ro и существующие методы чтения.

## Риски

| Приоритет | Подтверждение | Сценарий / доказательство | Итог / действие |
|---|---|---|---|
| P0 | Предположение, факт двойной отправки не установлен | Последний GET Draft и PUT sendAt не атомарны: control_mailing.py cycle, строки 284–291; внешний писатель EspoCRM не использует локальные flock | Не исправлять ночью; перед расширением пилота исключить конкурирующих писателей, воспроизвести гонку на FakeAPI, согласовать серверную защиту |
| P1 | Подтверждено кодом | Рестарт переводит running→failed, queued/pending→cancelled: control_jobs.py:76–77; close:299 отменяет очередь и дочерние процессы | Запуск только после подтверждения отсутствия активных jobs; восстановления задач автоматически нет |
| P1 | Подтверждено кодом | Старт пересохраняет review и может удалять прежние PDF/HTML: control_jobs.py:80–87, control_offers.py:21,89 | Не запускать для диагностики; перед будущим стартом согласованный backup и проверка ссылок |
| P1 | Подтверждено, исправлено отображение | Отказ API до slots вызывал UnboundLocalError, некорректные слоты учитывались в запасе: control_mailing.py:320 html | Инициализация/сброс состояния, проверка уникальности и графика; worker не менялся |
| P1 | Подтверждено, исправлено | При ошибке CRM-истории crm_remaining='—' сравнивался с int: control_mvp.py:746 home | Кнопка включается только при известном числовом остатке |
| P1 | Подтверждено | Backup от 03.11 не содержит followup.sqlite3/control_followup.py; оба tar не содержат полного прежнего mailer и espo-export.json | Старые архивы не являются полным актуальным recovery set; см. recovery |
| P1 | Подтверждённое ограничение | Архив рабочего дерева создан при изменении .control-center-state; CRC не доказывает согласованность SQLite и journals | Нужен согласованный recovery rehearsal вне production; не восстанавливать состояние поверх живого |
| P1 | Подтверждено | В compact старый шаблон с прежними брендом и сроком; wrapper исправляет их только на пути Control Center | Прямые генерации compact не принимать как актуальные; исходник compact не менять в этой задаче |
| P1 | Подтверждено кодом | Входящие ответы/bounce не читаются; локальный do_not_contact блокирует только followup: control_followup.py:246,277 | Перед контактом проверить CRM; отказ вручную обработать в CRM и рассылке отдельным разрешённым процессом |
| P2 | Подтверждено кодом | Mailing.html делает полный all_rows и GET каждого письма без кеша; all_rows до 100000 строк: control_mailing.py:44,320 | На большой CRM UI может ждать много таймаутов; измерить на хосте, затем общий ограниченный GET-кеш |
| P2 | Подтверждено кодом | Последний результат выбирается по rowid DESC, не ended/dateSent: control_analysis.py:45; control_leads.py:71 | Для текущей последовательной очереди ожидаемо; при импорте не переставлять rowid |
| P2 | Подтверждено | Индекс имеет лимиты, снимок сообщил 3 предупреждения; workflow ~1.6 с и повторно читает досье | Счётчики относятся к доступному индексу; до роста базы измерить и ограничить затраты |
| P2 | Подтверждено | Три дня — пн–пт, без праздничного календаря: control_followup.py:42 | Уточнена подпись UI; алгоритм оставлен прежним |

Защиты, найденные в коде: worker.lock не допускает второго Jobs на том же state-dir; mailing.lock и .mailer.lock сериализуют циклы; ключ очереди — Email ID; intent сохраняется до PUT/POST; неизвестный PUT без подтверждённого sendAt не повторяется. Это не доказательство отсутствия всех гонок между checkout, ручными действиями и CRM. Mailing.start сам по себе не защищён от повторного вызова, но main вызывает его один раз, а циклы сериализуются. Дубли задач блокируются повторной проверкой активных доменов под jobs.lock и транзакцией submit_analysis; новые сайты дополнительно исключают всю историю. Ничего из этого ночью не изменено.
