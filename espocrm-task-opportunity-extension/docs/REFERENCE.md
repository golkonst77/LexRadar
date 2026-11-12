# Дополнение к справочникам EspoCRM / LexRadar — 11.11.2026

Область: только автономный комплект `espocrm-task-opportunity-extension`.
Перед разработкой прочитаны справочники проекта `docs/espocrm_lexradar_reference.md`
и `docs/espo-integration.md`, а также продуктовый план. По прямому требованию
не менять LexRadar-web-control исходные справочники не редактировались; новое
решение и его ограничения зафиксированы здесь вместе с кодом комплекта.
Это не изменение ранее проверенной схемы экспорта и не заявление о живой приёмке.

Новая схема (применяется только при явной установке оператором):

- Custom button Task: `lrCreateOpportunity` → `custom:lexradar/task-opportunity`.
- GET/POST `/LexRadar/Task/:id/opportunity` → `TaskOpportunityAction`.
- Opportunity: `cLrSourceTask` belongsTo Task, столбец `c_lr_source_task_id`,
  UNIQUE индекс без deleted. Task и её parent не изменяются.
- Новая сделка: Task read + Opportunity create, доступ к Account/User, активный
  assignedUser Task, сохранённый callResult «Заинтересовались», точный marker.
- Account: parent Account, fallback старый accountId без другого parent;
  конфликт блокирует. Amount/closeDate вводит пользователь; stage берётся
  точным хранимым значением «LexRadar · Клиент заинтересован».
- Сервер: SchemaGuard → транзакция → FOR UPDATE Task → блокирующий поиск
  Opportunity с deleted → стандартный Record Service create → commit.
- Hook beforeSave сохраняет неизменность source; UNIQUE защищает параллельную
  запись. Обычные сделки без source не сопоставляются с Task автоматически.
- JSON merge только добавляет отсутствующие ключи/элементы; конфликт — отказ.
  Нет изменения callResult, stage options/probability, чужих переводов/layouts.

Совместимость сверена с публичным исходным тегом EspoCRM 10.0.6 (2e092e3):
Action/Request/ResponseComposer, Record Service create/read и CreateResult,
ORM TransactionManager/SelectBuilder/Repository clone + forUpdate, Hook BeforeSave,
ActionHandler и views/modals/edit.setupLate/createRecordView.
`Record Service::create` используется так же, как в штатном Lead ConvertService.

Гарантия не распространяется на исторические сделки без source, физическое
удаление строк, прямой SQL и отключение серверных защит администратором.
Сторонние hooks/формулы проверяются на клоне. Файловый стенд не доказывает
транзакционные свойства рабочей БД; обязательны rebuild и verify-schema.

Секреты не используются. Подключений к рабочей CRM, установок, сделок,
писем, изменений очереди/сервисов и фоновых запусков не было.


### Исправление кнопки «Создать сделку», 12.11.2026

В комплекте espocrm-task-opportunity-extension исправлено ошибочное cCallResult
на существующее Task.callResult в Policy, ACL действия и hook. GET остаётся
предпросмотром организации; запись создаётся только POST после сохранения формы.
Маршрут GET/POST и клиентский обработчик сохранены. Ошибки Policy передаются
через BadRequest::createWithBody: ASCII reason и JSON message в UTF-8 вместо
русского X-Status-Reason. Схема БД и рабочая CRM не менялись.
Пройдены точечные offline policy/action/hook/frontend проверки, включая GET
без создания записи и открытие формы с Account; рабочая CRM не проверялась.
Установка и deploy.sh не запускались. Для обновления уже установленного комплекта
нужно заменить только Policy.php, TaskOpportunityAction.php и LexRadarSource.php,
затем clear_cache.php (без rebuild). Обычный install.php отклоняет отличающийся
установленный код; для этого исправления его повторно не использовать.


### Уточнение поля результата в расширении, 12.11.2026

По явному указанию оператора Policy читает `$task['cCallResult']`.
Это уточнение заменяет предыдущее утверждение о Task.callResult только для
комплекта espocrm-task-opportunity-extension. Проверки ACL действия и hook
используют то же cCallResult; существующие offline-фикстуры приведены к этому
полю, включая отказ при наличии только callResult. README и verify-schema.php
уже требуют cCallResult. Рабочая схема, CRM, БД и deploy.sh не запускались
и не изменялись; подтверждение на рабочей CRM в эту задачу не входит.

Проверка сохранения в Git: PHP 8.4.27 `-n -l` всех 10 PHP-файлов и
существующие offline policy/action/hook проверки пройдены. Синтаксис новых
и изменённых Python-файлов проверен без исполнения. Деплой не запускался.
