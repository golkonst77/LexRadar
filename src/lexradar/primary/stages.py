"""Working stage structure from the PR #10 task, not a verbatim v2.5 registry."""

from dataclasses import dataclass


@dataclass(frozen=True)
class StageDefinition:
    stage_id: str
    title: str
    original_sections: tuple[str, ...]
    domains: tuple[str, ...] = ("personal_data",)


STAGES = (
    StageDefinition("01", "Идентификация сайта, организации и оператора", ("v2.5 §9 этапы 1–2",)),
    StageDefinition("02", "Все юридические лица и ИП", ("v2.5 §3; §9 этап 2",)),
    StageDefinition("03", "Структура сайта и полнота обследования", ("v2.5 §9 этап 1; §12 А",)),
    StageDefinition("04", "Политика обработки персональных данных", ("v2.5 §9 этап 3",)),
    StageDefinition("05", "Формы сбора данных", ("v2.5 §9 этап 4",)),
    StageDefinition("06", "Согласия на обработку персональных данных", ("v2.5 §9 этап 4",)),
    StageDefinition("07", "Отдельные согласия на рекламу", ("v2.5 §9 этапы 4 и 9",)),
    StageDefinition("08", "Cookies, аналитика и сторонние сервисы", ("v2.5 §9 этапы 5–6",)),
    StageDefinition(
        "09",
        "Получатели, маршрутизация и возможная трансграничная передача",
        ("v2.5 §9 этапы 10–11",),
    ),
    StageDefinition(
        "10",
        "Фото, отзывы и специальные категории данных",
        ("v2.5 §9 этапы 7 и 9",),
        ("personal_data", "medical", "consumer"),
    ),
    StageDefinition("11", "Публичные сведения об операторе, включая РКН", ("v2.5 §9 этап 8",)),
    StageDefinition(
        "12",
        "Медицинские лицензии, услуги и раскрытия",
        ("v2.5 §9 этап 12",),
        ("medical", "paid_medical", "consumer"),
    ),
    StageDefinition(
        "13",
        "Итоговая оценка, пробелы доказательств и дальнейшие проверки",
        ("v2.5 §11–13; исходный этап 13 требует отдельной сверки",),
        ("personal_data", "medical", "paid_medical", "consumer", "other"),
    ),
)

STATUS_SCOPE = (
    "Статус относится только к проверенным локальным материалам; "
    "no_issue_observed не является юридической гарантией соответствия"
)
