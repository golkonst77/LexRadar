# Точечные исправления КП, 30.10.2026

Control Center передаёт Parser `name` в `company_name` входного CSV. Его отдельный
процесс анализа адаптирует результат штатного render_client до вызова штатного
write_html_pdf: меняются только адресат и вступительный абзац. Исходный шаблон,
цены, санкции и CSS сохранены. LexRadar-compact не изменён.

Коммерческий READY/HOLD/REJECT вычисляется при чтении сохранённого результата:
подтверждённые правила с доказательствами и содержательным HTML/PDF дают READY;
помеченные manual_review или неподтверждённые признаки дают HOLD; пустой список
признаков даёт REJECT. Ошибки/отсутствие материалов требуют HOLD для проверки.
Единственная прежняя причина manual_review_required_including_unverified_constant_block4
не блокирует READY. Внутренние qualification, release flags и CSV движка сохранены.
READY обозначает коммерческую готовность, отправки нет.

Сохранённая Клиника «А», задание 79bc04894af64fae8e78036852da7804:
SUCCESS остаётся SUCCESS; коммерческий результат READY по правилу 1 (сроки хранения
в полном читаемом тексте политики). После перезапуска веб-службы достаточно
обновить страницу. Старый PDF при этом не меняется.

## Новый PDF без сканирования

На хосте от golkonst, из корня checkout, выполнить команду ниже. Она создаёт
отдельную версию HTML/PDF в новом каталоге, сохраняя прежние артефакты и их
привязки. Старая ссылка панели по-прежнему открывает прежний PDF; для отправки
использовать новый файл по напечатанному пути. Автоматической подмены PDF нет.

```bash
/home/golkonst/src/LexRadar/.venv/bin/python -B - <<'PY'
import json, sqlite3, sys, tempfile
from pathlib import Path
from control_analysis_runner import ENGINE, personalize
sys.path.insert(0, str(ENGINE / 'src'))
from lexradar.pdf_export import write_html_pdf
state = Path('.control-center-state').resolve()
job = '79bc04894af64fae8e78036852da7804'
with sqlite3.connect(f'file:{state / "jobs.sqlite3"}?mode=ro', uri=True) as db:
    params = json.loads(db.execute('SELECT params FROM jobs WHERE id=?', (job,)).fetchone()[0])
source = state / job / 'analysis/sites/row-000002/proposal.html'
page = personalize(source.read_text(encoding='utf-8-sig'), params.get('name', ''))
output = Path(tempfile.mkdtemp(prefix='proposal-updated-', dir=state / job))
(output / 'proposal.html').write_text(page, encoding='utf-8')
write_html_pdf(page, output / 'proposal.pdf')
print(output / 'proposal.pdf')
PY
```

## Применение

Правки уже находятся в checkout ветки web-control-development. Перезапуск:

```bash
su - -c 'systemctl restart lexradar-web.service'
su - -c 'systemctl status lexradar-web.service --no-pager'
```

Пять точечных unittest прошли. Штатная генерация на сохранённом досье проверена
до входа write_html_pdf: HTML содержит новый адресат и дословное вступление.
SHA-256 исходных артефактов не изменились. Проверка реального Chromium PDF в
песочнице остановилась на Operation not permitted; на хосте требуется выполнить
команду выше и проверить PDF. Полный pytest и сетевой сбор не запускались.
В test_control_enrichment дополнительно обнаружены два прежних падения проверок
объединения лидов/отображения email; воспроизведены на исходном HEAD, вне задачи.

Коммит в среде Codex не создан: `.git` смонтирован только для чтения
(`Unable to create .git/index.lock: Read-only file system`). На хосте:

```bash
git add control_analysis.py control_analysis_runner.py control_enrichment.py tests/test_control_analysis.py tests/test_control_enrichment.py docs/control-center-proposal-fixes.md
git commit -m 'Fix Control Center proposal personalization and commercial readiness'
```
