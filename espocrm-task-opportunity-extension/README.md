# LexRadar: «Создать сделку» из Task — EspoCRM 10.0.6

Готовый CLI-комплект установки. **Не устанавливался; рабочая CRM и её БД не вызывались.**
Все исходники, JSON-патчи, проверки и команды находятся в этом каталоге. Код
LexRadar-web-control, Parser v3, LexRadar-compact и внешние справочники не менялись.
Установка не отправляет писем и не создаёт сделок.

Это архив для установки через CLI, **не ZIP для загрузки в Administration → Extensions**.
CLI выбран для явного предварительного просмотра и безопасного объединения уже
существующих `Task.json`, `Opportunity.json`, переводов и маршрутов. Не копируйте
каталог `patches` поверх CRM через `cp`/`docker cp`.

## Поведение

Кнопка добавляется штатным `clientDefs.Task.menu.detail.buttons` с `__APPEND__`,
`handler`, `actionFunction`, `checkVisibilityFunction`. Она видна в сохранённой
карточке «Контрольный звонок LexRadar» или «Контрольный звонок LexRadar · …»,
если разрешены чтение Task и создание Opportunity.

Сервер для новой сделки требует:

- точное имя указанного семейства задач и первую строку `LexRadar followup Email/<ID>`;
- сохранённый `Task.cCallResult = Заинтересовались`;
- организацию из `parentType=Account/parentId`; для старых задач допустим `accountId`
  без другого родителя; конфликт двух связей блокирует создание;
- активного ответственного из Task.assignedUserId, без подстановки Admin/текущего пользователя;
- доступ пользователя к Task, Account, User и созданию Opportunity;
- существующее **хранимое значение enum** `LexRadar · Клиент заинтересован`.

Если сделка уже есть, открывается она, даже если результат звонка позднее изменился.
Если её удалили или пользователь не может её прочитать — отказ, без новой сделки.
Для новой сделки открывается штатная модальная форма Opportunity с layout `detail`:
организация, ответственный и стадия заполнены и заблокированы; сумма и дата закрытия
очищены после штатного заполнения defaults. Пользователь заполняет их и валюту.
Сервер повторно проверяет обязательные значения и штатные правила CRM, включая
права назначения, связи, required/custom fields, validators, hooks и формулы.

Связь хранится в `Opportunity.cLrSourceTask` → Task и уникальном
`cLrSourceTaskId`. Task.parent/Account и остальные поля Task не изменяются.
Поле служебное и автоматически в layout не добавляется. Его можно добавить для
просмотра в layout Opportunity вручную; менять/очищать связь нельзя.
Стадии, вероятности, переводы, существующие пользовательские поля и layouts
не заменяются. Поле cCallResult и стадия не создаются — они должны уже существовать.

## Серверная защита и границы

`GET/POST /api/v1/LexRadar/Task/<id>/opportunity` — обычная авторизация текущего
пользователя CRM. API-ключ LexRadar в коде и браузере не используется.

Сохранение формы направлено на POST расширения. Операция открывает транзакцию,
читает исходную Task через `SELECT … FOR UPDATE`, затем делает блокирующий поиск
сделки, включая soft-deleted. При повторе возвращает существующую запись. При
отсутствии создаёт через штатный Record Service и фиксирует сделку вместе со связью
одним commit. При исключении — rollback. Потерянный ответ после commit безопасен:
следующее сохранение возвращает прежний ID; перед commit — откат и возможность повтора.

Окончательный барьер — UNIQUE по **одному** `opportunity.c_lr_source_task_id`,
без `deleted` в индексе. Он защищает и параллельные обычные API/import записи,
передающие исходную Task. Hook проверяет новые связи и запрещает смену/очистку
исходной Task у сохранённой сделки. Штатный relation API EspoCRM не разрешает
link/unlink для belongsTo. Обычный POST с тем же source ID может получить конфликт,
но второй связанной сделки не создаст. Idempotent-ответ обеспечивает именно маршрут
расширения. Удаление сделки не освобождает source ID.

Гарантия относится к сделкам с сохранённым source ID. Сделку, вручную созданную без
связи с Task, или историческую сделку прежней интеграции автоматически сопоставить
невозможно. Здесь нет поиска по похожему имени или только Account; исторические
связи требуют отдельной сверки, автоматической миграции нет. Другие расширения,
прямой SQL, отключение hooks, физическое удаление записей и внешние побочные эффекты
существующих hooks находятся вне гарантии. Сторонние hooks нельзя сделать
транзакционными этим комплектом; на тестовом клоне проверить их отдельно.

Требуются MySQL/MariaDB, InnoDB у task/opportunity и работающий UNIQUE индекс.
SchemaGuard проверяет их перед каждым вызовом и созданием со source ID: при
отсутствии — отказ. PostgreSQL этой версией комплекта не поддерживается.

Стандартная **отдельная полная страница** создания могла бы обойти специальный
POST. Минимальный надёжный вариант реализован: штатная форма в модальном окне,
кнопка Full Form скрыта, изменён URL только её экземпляра модели. Глобальные
представления и стандартное создание обычных Opportunity не переопределяются.

## Локальные проверки без CRM

Нужны PHP CLI 8.3+ с pcntl (Linux), Node.js и Python 3. Зависимости приложения
не устанавливаются. Проверки не используют bootstrap CRM и не открывают сеть:

```bash
cd /home/golkonst/src/LexRadar-web-control/espocrm-task-opportunity-extension
./check.sh
```

Для другого PHP: `PHP_BIN=/path/to/php ./check.sh`. Скрипт проверяет PHP/JS-синтаксис,
Policy, настоящий класс серверной операции с поддельными API/ORM, восемь параллельных
процессов с файловой транзакцией, rollback, повтор, потерянный ответ, ACL,
подменённые связи, tombstone, настоящий hook, установщик на временных файлах,
сохранность чужих JSON-ключей, конфликты и откат, клиентский обработчик.
**Это не тест InnoDB и не приёмка интерфейса CRM.**

Сборка переносимого комплекта:

```bash
python3 -B build.py
sha256sum -c dist/SHA256SUMS
```

## Безопасная установка оператором

Команды ниже подготовлены, но здесь не выполнялись. Сначала выполнить на
изолированном клоне LXC с отдельной БД, отключёнными cron/daemon/SMTP и закрытой
сетью отправки. Не запускать клонированные фоновые очереди против рабочей БД.

### 1. Перенести пакет на Proxmox

После локальных проверок и сборки, с машины проекта:

```bash
scp dist/lexradar-task-opportunity-1.0.0.tar.gz root@192.168.233.100:/root/
```

Далее команды из root shell **Proxmox**, не внутри Docker:

```bash
pct status 102
pct config 102
mkdir -p /var/lib/vz/dump
vzdump 102 --mode stop --compress zstd --dumpdir /var/lib/vz/dump
```

Это короткий согласованный останов всего LXC для резервной копии приложения и БД.
Убедиться в успешном завершении и сохранить полученный путь архива. В `pct config`
проверить все mount points: bind mounts и диски с `backup=0` могут не войти в vzdump;
при наличии таких дисков отдельно включить их согласованную резервную копию.
Восстанавливать сначала в **новый свободный VMID**, не поверх рабочего 102.

```bash
pct push 102 /root/lexradar-task-opportunity-1.0.0.tar.gz /root/lexradar-task-opportunity-1.0.0.tar.gz
pct enter 102
```

### 2. Инвентаризация и сохранение вне Docker

Теперь shell **LXC 102**. Не выводить полный inspect/config в чат: там могут быть секреты.
Сначала закрыть пользовательский доступ на время установки штатным reverse proxy.
Приостановить все cron/daemon процессы, пишущие в CRM, штатным способом; не запускать
второй экземпляр. Ни одна команда комплекта не меняет cron или почтовую очередь.

```bash
umask 077
LR_RUN=$(date -u +%Y%m%dT%H%M%SZ)
LR_SAFE=/root/lexradar-extension-$LR_RUN
mkdir -m 700 "$LR_SAFE"
docker inspect espocrm > "$LR_SAFE/espocrm-inspect.private.json"
docker inspect espocrm-db > "$LR_SAFE/db-inspect.private.json"
docker inspect --format '{{json .Mounts}}' espocrm
docker exec espocrm php -r '$c=require "/var/www/html/data/config.php"; echo ($c["version"] ?? "unknown"), PHP_EOL;'
docker stop espocrm
docker cp espocrm:/var/www/html "$LR_SAFE/html-before"
docker commit espocrm "lexradar/espocrm-before:$LR_RUN"
docker image save "lexradar/espocrm-before:$LR_RUN" | gzip > "$LR_SAFE/image-before.tar.gz"
docker start espocrm
mkdir -p /opt/lexradar-extensions
tar -xzf /root/lexradar-task-opportunity-1.0.0.tar.gz -C /opt/lexradar-extensions
LR_PKG=/opt/lexradar-extensions/espocrm-task-opportunity-extension
docker cp "$LR_PKG" espocrm:/tmp/lexradar-task-opportunity-extension
docker exec espocrm mkdir -p /var/backups/lexradar
docker exec espocrm chown www-data:www-data /var/backups/lexradar
```

Резервная копия vzdump — источник согласованной БД; `docker commit` **не сохраняет
данные внешних volumes**, не заменяет резервную копию БД и не является сам по себе
способом миграции. После остановки/старта проверить штатное состояние контейнера;
пользовательский доступ и фоновые писатели пока не возобновлять.

### 3. Предварительный просмотр и применение

```bash
docker exec -u www-data espocrm php /tmp/lexradar-task-opportunity-extension/install.php --root=/var/www/html
```

Проверить список: только namespace LexRadar, два JS, точечные metadata/i18n/routes.
Любой конфликт прекращает установку до записи. Не исправлять конфликт перезаписью
существующего JSON. Установщик проверяет версию 10.0.6, не подключается к БД,
создаёт резервную копию **всех** меняемых файлов до первой записи и атомарно заменяет
каждый файл. Весь набор файлов не является одной файловой транзакцией, поэтому
окно обслуживания обязательно. Повтор успешной установки — no-op.

```bash
docker exec -u www-data espocrm php /tmp/lexradar-task-opportunity-extension/install.php --root=/var/www/html --apply --backup="/var/backups/lexradar/$LR_RUN"
docker cp "espocrm:/var/backups/lexradar/$LR_RUN" "$LR_SAFE/installer-backup"
docker exec -u www-data -w /var/www/html espocrm php rebuild.php
docker exec -u www-data espocrm php /tmp/lexradar-task-opportunity-extension/verify-schema.php /var/www/html
```

Rebuild — штатная операция EspoCRM, применяет metadata/schema, может затронуть
ранее ожидавшие изменения администратора. Сначала принять на клоне. Успешный
`verify-schema.php` необходим до открытия доступа; он только читает БД/metadata,
не создаёт сделок. Ошибка enum означает необходимость проверки **реального хранимого
значения**, а не разрешение автоматически добавить или переименовать стадию.

### 4. Сохранность при пересоздании Docker

При отсутствии mounts файлы остаются только в writable layer контейнера и
потеряются при его удалении. После принятой установки снова остановить писателей,
закрыть доступ и сохранить установленное состояние вне контейнера:

```bash
docker stop espocrm
docker cp espocrm:/var/www/html "$LR_SAFE/html-after"
docker commit espocrm "lexradar/espocrm-lexradar:$LR_RUN"
docker image save "lexradar/espocrm-lexradar:$LR_RUN" | gzip > "$LR_SAFE/image-after.tar.gz"
docker start espocrm
```

Архивы содержат приватную конфигурацию — хранить 0700/0600, не помещать в Git.
До изменения штатного deployment **не пересоздавать контейнер из прежнего образа**.
Сохранить `$LR_SAFE` также на отдельном носителе.

Для постоянной установки подготовить именованный volume, заполненный всей текущей
`/var/www/html`, и включить его в существующий Compose/run-конфиг. На остановленном
приложении можно подготовить volume следующими командами (в LXC; сеть не нужна):

```bash
docker volume create espocrm-html-lexradar
tar -C "$LR_SAFE/html-after" -cf "$LR_SAFE/html-after.tar" .
docker run --rm --network none --user 0 --entrypoint sh \
  --mount type=volume,src=espocrm-html-lexradar,dst=/restore \
  --mount "type=bind,src=$LR_SAFE,dst=/backup,readonly" \
  "lexradar/espocrm-lexradar:$LR_RUN" \
  -c 'test -z "$(ls -A /restore)" && tar -xpf /backup/html-after.tar -C /restore'
```

В штатном deployment добавить `espocrm-html-lexradar:/var/www/html` (Compose:
`external: true` для этого volume), сохранив прежние image/env/network/ports/DB/cron.
Точную команду пересоздания нельзя безопасно восстановить только из IP и имени
контейнера: исходный deployment не предоставлен. Не удалять исходный контейнер до
приёмки копии. После согласованного переключения повторить `verify-schema.php`,
проверить пользовательские файлы и mounts; новые изменения после `html-after`
требуют свежего согласованного копирования. Этот комплект deployment сам не меняет.

## Приёмка на клоне CRM

Проверить под обычным пользователем и ограниченной ролью:

1. Кнопка в detail Task и отсутствие её у других задач; несохранённый результат
   звонка не используется. Ошибка до сохранения «Заинтересовались» объяснима.
2. Правильные Account и назначенный User; конфликт parent/account блокирует.
3. Сумма и дата пустые, обе обязательны; работают валюта, required custom fields,
   текущие layouts/формулы, stage/probability и действующие права назначения.
4. Создание одной тестовой сделки **на клоне**, сохранённый `cLrSourceTaskId`,
   неизменные Task.parent/description/status. После сохранения открывается сделка.
5. Повтор, две вкладки/два пользователя с одновременным Save, потеря ответа после
   POST: один ID и одна строка по source ID. Проверить на настоящем InnoDB.
6. Нет Task read / Opportunity create / доступа к Account/User / доступа к уже
   созданной сделке: отказ без утечки полей и без новой сделки.
7. Soft delete сделки блокирует повтор. Смена/очистка source через PUT отклоняется.
8. Другие custom buttons, поля, стадии, переводы и обычные сделки продолжают работать.
9. Нет ошибок в `data/logs/`, браузерной консоли, rebuild; подтверждён UNIQUE индекс.
10. После переключения deployment: mount сохраняет файлы при пересоздании контейнера.

Затем жёсткая перезагрузка браузера. Только после приёмки открывать доступ и
возобновлять ранее работавшие фоновые процессы штатным способом. Сам комплект
не включает отправку, но запуск прежних процессов может продолжить их обычную работу.

## Откат

**После появления сделок** — выключить только кнопку и маршруты, сохранив столбец,
индекс, связь и защитный hook. Не удалять entityDefs поля и не запускать rebuild
с прежним Opportunity.json: это может уничтожить данные связи.

```bash
docker exec -u www-data espocrm php /tmp/lexradar-task-opportunity-extension/install.php --root=/var/www/html --disable
docker exec -u www-data espocrm php /tmp/lexradar-task-opportunity-extension/install.php --root=/var/www/html --disable --apply --backup="/var/backups/lexradar/disable-$LR_RUN"
docker cp "espocrm:/var/backups/lexradar/disable-$LR_RUN" "$LR_SAFE/disable-backup"
docker exec -u www-data -w /var/www/html espocrm php rebuild.php
```

Обновить постоянный volume/образ резервирования после отключения. Для повторного
включения использовать обычный plan/apply с **новым** backup path.

**Только до первого использования**, когда новых связей точно нет, возможен полный
файловый откат. Он сверяет SHA-256 всех установленных файлов и откажет, если их
успели поменять. При частичной установке или более поздних изменениях сверять
`manifest.json` и приватные before-копии вручную; не перезаписывать чужую работу.

```bash
docker exec -u www-data espocrm php /tmp/lexradar-task-opportunity-extension/install.php --root=/var/www/html --rollback --backup="/var/backups/lexradar/$LR_RUN"
docker exec -u www-data espocrm php /tmp/lexradar-task-opportunity-extension/install.php --root=/var/www/html --rollback --apply --backup="/var/backups/lexradar/$LR_RUN"
docker exec -u www-data -w /var/www/html espocrm php rebuild.php
```

Катастрофическое восстановление: использовать холодный vzdump в новом VMID,
в закрытой сети и без фоновых отправок; согласовать момент восстановления БД и
файлов, чтобы не потерять последующие записи. Шаблон для **свободного** VMID 9102:
`pct restore 9102 /var/lib/vz/dump/ИМЯ_ПРОВЕРЕННОГО_АРХИВА.tar.zst --storage ИМЯ_ХРАНИЛИЩА`.
Не выполнять поверх 102. Названия архива/хранилища определяет оператор.

## Источники совместимости

- [Custom buttons](https://docs.espocrm.com/development/custom-buttons/)
- [API actions](https://docs.espocrm.com/development/api-action/)
- [Исходники тега 10.0.6](https://github.com/espocrm/espocrm/tree/10.0.6)
- [Стандартная форма](https://github.com/espocrm/espocrm/blob/10.0.6/client/src/views/modals/edit.js)
- [Record Service](https://github.com/espocrm/espocrm/blob/10.0.6/application/Espo/Core/Record/Service.php)
- [Транзакции ORM](https://github.com/espocrm/espocrm/blob/10.0.6/application/Espo/ORM/TransactionManager.php)

Справочное дополнение этого отдельного комплекта: [docs/REFERENCE.md](docs/REFERENCE.md).
Полный перечень файлов и хешей: `FILES.sha256`; результаты локальной проверки:
[docs/CHECKS.md](docs/CHECKS.md).
