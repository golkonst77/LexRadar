"""Evidence-backed internal audit and conditional proposal, without legal assertions."""

import json
from pathlib import Path

from .commercial_preview import esc, write_document
from .pdf_export import write_pdf


def generate(root: Path, *, replay_source=None, pdf=False, font_path=None):
    collection = json.loads((root / "dossier/collection.json").read_text())
    primary = json.loads((root / "analysis/primary.local.json").read_text())
    lines = [
        "LexRadar — предварительный аудит сайта",
        collection["target_url"],
        f"Дата исходного сбора: {collection['started_at']}",
        f"Дата анализа: {primary['assessed_at']}",
        "HOLD / not_confirmed. Внутренний черновик для проверки специалистом.",
        "Юридические нарушения не установлены. Полное соответствие также не подтверждено.",
        "AI-ответ синтетический; независимая LLM-экспертиза не выполнялась. Стоимость API: 0.",
    ]
    if replay_source:
        lines += [
            f"Повторный анализ сохранённого досье: {replay_source}.",
            "Свежий доступ к сайту этим отчётом не подтверждён. Состояние сайта могло измениться.",
        ]
    lines += [
        "",
        "1. Охват и технические факты",
        f"Страниц: {len(collection['pages'])}; документов: {len(collection['documents'])}; "
        f"форм в статическом разборе: {len(primary['forms'])}.",
        "Наблюдаемые декларации не доказывают фактическую передачу данных.",
    ]
    for evidence in collection["evidence"]:
        lines.append(f"{evidence['id']}: {evidence['source']}; {evidence['status']}")
        for artifact in evidence["artifacts"]:
            lines.append(f"  dossier/{artifact['path']} SHA256 {artifact['sha256']}")
    lines += ["", "2. Выборка наблюдений с цитатами (полный перечень — в primary.local.json)"]
    # Select different stages; never present a negative inventory as a proven omission.
    seen = set()
    for observation in primary["observations"]:
        stage = observation["stage_id"]
        citations = [c for c in observation["citations"] if c.get("quote")]
        if stage in seen or not citations:
            continue
        seen.add(stage)
        citation = citations[0]
        lines += [
            f"{observation['id']}, этап {stage}: {observation['description']}",
            f"Источник {citation['evidence_id']}: {citation['url']}",
            f"Основа цитаты: {citation['basis']}; страница/сегмент: {citation.get('page')}",
            "Цитата (начало, до 500 символов): «" + citation["quote"][:500] + "»",
            f"Артефакт: dossier/{citation['artifact_path']}; SHA256 {citation['artifact_sha256']}",
        ]
    lines += ["", "3. Юридические замечания — вопросы, требующие проверки"]
    for stage in primary["stages"]:
        lines += [f"Этап {stage['stage_id']}: {stage['title']}; статус {stage['status']}."]
    lines += [
        "При обработке данных через формы: установить оператора, цели, правовое основание,",
        "получателей и сроки; сопоставить документы с реальными процессами и интерфейсом.",
        "Для медицинского сайта: отдельно проверить применимость требований к раскрытию",
        "сведений о лицензии, услугах и платной медицинской помощи по актуальным первоисточникам.",
        "Обнаруженное упоминание лицензии не удостоверяет её действительность или объём.",
        "Актуальные редакции норм и их применимость в этом запуске не подтверждались.",
        "",
        "4. Ограничения",
        "Статический разбор может быть частичным; отсутствие элемента в выборке не доказывает",
        "его отсутствие на сайте. JavaScript, визуальное состояние и отправка форм не проверены.",
        "Извлечение PDF не равно визуальному прочтению всего документа.",
    ]
    lines += list(dict.fromkeys(collection["warnings"] + primary["limitations"]))[:25]
    lines += [
        "Полные ограничения каждого материала, этапа и гипотезы сохранены в JSON досье и анализа.",
        "",
        "5. Коммерческое предложение — условный состав работ",
        "Предмет: проверка процессов и, по согласованию, подготовка документов и инструкции",
        "вебмастеру. Основанием предложения не является утверждение о нарушении.",
        "Этап 1: интервью с ответственным лицом, проверка форм и получателей, уточнение оператора.",
        "Результат: согласованная матрица процессов, документов и открытых вопросов.",
        "Этап 2: после подтверждения необходимости — политика, применимые согласия,",
        "медицинские раскрытия и другие согласованные документы; инструкция размещения.",
        "Этап 3: контроль размещения по отдельному согласованию; внедряет вебмастер заказчика.",
        "Стоимость и сроки не утверждены: рассчитываются после этапа 1 и согласуются до работ.",
        "Для оценки нужны: реквизиты оператора, перечень форм и сервисов, действующие документы,",
        "описание хранения данных и договоров с обработчиками. Заполненные анкеты не требуются.",
        "Предложение — внутренний проект; клиенту не отправлялось, публичной офертой не является.",
        "",
        "6. Воспроизводимость",
        "dossier/collection.json — исходный манифест; dossier/artifacts/ — сохранённые байты.",
        "analysis/primary.local.json — факты, гипотезы, 13 этапов и ограничения.",
        "analysis/trace-map.local.json — карта цитат; sha256.json — контрольные суммы.",
    ]
    (root / "audit.txt").write_text("\n".join(lines), encoding="utf-8")
    cards = "".join("<p>" + esc(line) + "</p>" for line in lines)
    write_document("<article>" + cards + "</article>", root / "audit.html")
    if pdf:
        write_pdf(lines, root / "audit.pdf", font_path=font_path)
