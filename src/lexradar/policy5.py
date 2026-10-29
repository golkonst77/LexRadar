"""Five bounded policy-content checks on complete saved text, never keyword absence."""

import hashlib
import re
from urllib.parse import urlsplit

CHECKS = {
    "operator": (
        "Оператор",
        r"(?:оператор(?:ом)?(?:\s+персональных\s+данных)?\s*[:—-]\s*[^.!?\n]{5,140})|\b(?:ООО|АО|ПАО|ИП|общество\s+с\s+ограниченной\s+ответственностью)\b\s*[«\"А-ЯЁA-Z][^\n]{3,180}",
    ),
    "purposes": (
        "Цели",
        r"(?:чтобы\s+(?:связаться|ответить|оформить|оказать)|(?:для|в\s+целях)\s+(?:ответа|связи|обработки\s+(?:заяв|обращ)|предоставления|исполнения|оказания|оформления|идентификации|направления|улучшения))|(?:цел[ьи](?:\s+(?:сбора|обработки)[^:\n.]{0,100})?|в\s+следующих\s+целях)\s*[:—-]\s*[^\n.]{12,}",
    ),
    "data": (
        "Перечень данных",
        r"имя|фамили[яи]|фио|номер[а]?\s+телефон|телефон[а-я]*\s*,|электронн[а-я]*\s+почт|контактн[а-я]*\s+(?:данн|сведен)|идентификационн[а-я]*\s+данн|IP[^\n]{0,12}адрес|данные\s+файлов.{0,20}Cookie",
    ),
    "retention": (
        "Сроки хранения",
        r"(?:хран[а-яё]*|использ[а-яё]*|доступн[а-яё]*|сохраня[а-яё]*|держим|оставляем)[^.!?]{0,160}(?:не\s+дольше|пока\s+(?:обрабатываем|рассматриваем|действует)|до\s+(?:завершения|исполнения|достижения|отзыва|отмены|удаления|окончания|предоставления)|в\s+течение\s+\d+|\d+\s*(?:дн|сут|месяц|лет|год))|(?:пока|покуда)\s+действует\s+договор|срок[а-я]*\s+хранения[^.!?]{0,100}(?:определяется\s+цел|исковой\s+давности|\d+\s*(?:дн|сут|месяц|лет|год))",
    ),
    "deletion": (
        "Прекращение и уничтожение",
        r"\b(?:когда|после|при|по\s+истечении|в\s+случае)\b[^.!?]{0,200}(?:стира(?:ются|ем|ют|ется)|удаля(?:ются|ем|ют|ется)|уничтожа(?:ются|ем|ют|ется)|подлежат\s+(?:уничтожению|удалению))|(?:стираем|стираются|удаляем|удаляются|уничтожаются|уничтожаем)[^.!?]{0,180}\b(?:когда|после|по\s+истечении|в\s+течение|при\s+отзыве|по\s+запросу)\b|(?:вправе|имеет\s+право|может)\s*:?\s*требовать[^.!?]{0,180}(?:удалени[ея]|уничтожени[ея])[^.!?]{0,40}в\s+случае",
    ),
}
POLICY_LABEL = re.compile(
    r"\bполитик[а-яё]*\b.{0,60}(?:конфиденц|персональн|данн)|privacy\s+policy", re.I
)
POLICY_PATH = re.compile(
    r"(?:^|/)(?:privacy(?:-policy)?|politi[ck][a-z_-]*|politika[^/]*)(?:[/.]|$)", re.I
)


def policy_materials(packet):
    """Resolve actual saved HTML/PDF policies, including nonstandard linked filenames."""
    if packet is None:
        return []
    data = packet.data
    target = getattr(data, "target_url", None)
    host = (
        urlsplit(str(target or data.pages[0].requested_url)).hostname
        if target or data.pages
        else None
    )

    def own(url):
        return (urlsplit(url).hostname or "").removeprefix("www.") == (host or "").removeprefix(
            "www."
        )

    urls = {
        link.url
        for page in data.pages
        for link in page.relevant_links
        if POLICY_LABEL.search(link.text) and own(link.url)
    }
    evidence = {item.id: item for item in data.evidence}
    result = []
    for material in [*data.pages, *getattr(data, "documents", [])]:
        url = str(material.final_url or material.requested_url)
        text = material.text or ""
        title = getattr(material, "title", "") or ""
        if (
            re.search(r"согласие|пользовательское соглашение", title, re.I)
            and not POLICY_LABEL.search(title)
            or re.match(r"\s*СОГЛАСИЕ\b", text, re.I)
        ):
            continue
        identified = own(url) and (
            url in urls
            or POLICY_PATH.search(urlsplit(url).path)
            or re.match(r"\s*ПОЛИТИКА\s+(?:КОНФИДЕНЦИАЛЬНОСТИ|ОБРАБОТКИ)", text, re.I)
        )
        if not identified:
            continue
        reading = material.reading
        available = evidence[material.evidence_id].available
        readable = (
            len(text.strip()) >= 500
            and len(set(re.findall(r"[а-яё]{3,}", text.lower()))) >= 15
            and "\ufffd" not in text
            and len(re.findall(r"[А-Яа-яЁёA-Za-z]", text)) >= len(text) * 0.4
        )
        complete = (
            available
            and readable
            and reading.provenance == "reproduced"
            and reading.text_coverage_complete
            and reading.text_truncated is False
            and getattr(material, "http_status", 200) not in {404, 410}
        )
        result.append(
            dict(
                material=material,
                url=url,
                text=text,
                complete=bool(complete),
                evidence=evidence[material.evidence_id],
            )
        )
    return result


def inspect_policy5(packet, *, _policies=None):
    """Absence is decided only over a complete readable policy, with exact coverage proof."""
    policies = policy_materials(packet) if _policies is None else _policies
    if len(policies) > 1:
        # Each legal entity/document receives its own five checks; no pooled text or verdict.
        return [row for policy in policies for row in inspect_policy5(packet, _policies=[policy])]
    result = []
    for key, (label, pattern) in CHECKS.items():
        candidates = []
        for policy in policies:
            text = policy["text"]
            match = re.search(pattern, text, re.I | re.S)
            if (
                match
                and key in {"operator", "purposes"}
                and re.search(
                    r"не\s+(?:указан|определ[её]н|раскрыт)|\[(?:укаж|наименов|заполн)",
                    match[0],
                    re.I,
                )
            ):
                match = None
            # Operator mentions confined to third-party services do not identify this operator.
            if key == "operator":
                matches = list(re.finditer(pattern, text, re.I | re.S))
                match = None
                for candidate in matches:
                    prefix = text[max(0, candidate.start() - 100) : candidate.end() + 200]
                    if not re.search(
                        r"(?:обработчик|сторонн[а-я]+\s+сервис|по\s+поручению|не\s+(?:указан|определ[её]н)|\[(?:укаж|наименов|заполн))",
                        prefix,
                        re.I,
                    ):
                        match = candidate
                        break
            status = "PASS" if match and policy["complete"] else "UNDEFINED"
            if not match and policy["complete"]:
                status = "FAIL"
            if match:
                start, end = match.span()
            else:
                anchors = {
                    "operator": r"оператор|общество",
                    "purposes": r"цел[ьи]|обработк",
                    "data": r"персональн[а-я]*\s+данн",
                    "retention": r"хран[а-яё]*[^.!?]{0,140}регламент[а-яё]*|срок[а-яё]*\s+хранения|хран[а-яё]*",
                    "deletion": r"отзыв|удаля[а-яё]*|уничтожа[а-яё]*",
                }
                anchor = (
                    re.search(r"хран[а-яё]*[^.!?]{0,140}регламент[а-яё]*", text, re.I)
                    if key == "retention"
                    else None
                )
                anchor = anchor or re.search(anchors[key], text, re.I)
                start = max(0, (anchor.start() if anchor else 0) - 50)
                end = min(len(text), start + 450)
            site_conflict = None
            if key == "operator" and match and policy["complete"]:
                declared = text[max(0, start - 50) : end + 300]
                inn = re.search(r"ИНН\s*:?\s*(\d{10}|\d{12})(?!\d)", declared)
                ogrn = re.search(r"ОГРН\s*:?\s*(\d{13})(?!\d)", declared)
                if inn and ogrn:
                    for entity in getattr(packet.data, "entities", []):
                        if (
                            str(entity.source) != policy["url"]
                            and entity.ogrn == ogrn[1]
                            and entity.inn
                            and entity.inn != inn[1]
                            and entity.inn in entity.raw_text
                            and ogrn[1] in entity.raw_text
                            and any(
                                entity.raw_text in (page.text or "")
                                for page in packet.data.pages
                                if str(page.final_url or page.requested_url) == str(entity.source)
                            )
                        ):
                            site_conflict = dict(url=str(entity.source), quote=entity.raw_text)
                            status = "FAIL"
                            break
            quote = text[start:end]
            material = policy["material"]
            page_texts = getattr(material, "page_texts", {})
            page_number = next((n for n, t in page_texts.items() if quote and quote in t), None)
            explanation = (
                "Найдена содержательная формулировка; это не заключение о полном соответствии закону."
                if status == "PASS"
                else f"Полный читаемый текст проверен: не раскрыты сведения по проверке «{label}». "
                "Проверялись синонимы и смысловые конструкции; отсутствие отдельного слова не использовалось."
                if status == "FAIL"
                else "Полного читаемого текста недостаточно для вывода об отсутствии сведений."
            )
            unlimited = re.search(r"Обработка\s+персональных\s+данных[^.]{0,160}без\s+ограничения\s+срока[^.]*\.", text, re.I)
            if key == "retention" and status == "FAIL" and unlimited:
                start, end = unlimited.span()
                quote = text[start:end]
                explanation = "Документ прямо устанавливает обработку без ограничения срока, без раскрытого условия окончания хранения. Другие документы оператора оцениваются отдельно; правовая квалификация требует ручной проверки."
            if site_conflict:
                explanation = "В сохранённых декларациях одного ОГРН разные ИНН: политика противоречит реквизитам сайта; внешняя проверка реестра не выполнялась."
            candidates.append(
                dict(
                    document_scope=policy["url"],
                    policy_operator=(
                        re.search(CHECKS["operator"][1], text, re.I)[0]
                        if re.search(CHECKS["operator"][1], text, re.I)
                        else None
                    ),
                    check=key,
                    label=label,
                    status=status,
                    compared_with=site_conflict,
                    confirmed=status == "FAIL",
                    source=policy["url"],
                    quote=quote,
                    explanation=explanation,
                    evidence_id=material.evidence_id,
                    page=page_number,
                    start=start,
                    end=end,
                    full_text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                    examined_chars=len(text),
                    complete_readable_text=policy["complete"],
                    artifacts=[
                        dict(path=a.path, sha256=a.sha256) for a in policy["evidence"].artifacts
                    ],
                )
            )
        # Any confirmed deficiency is retained; another document cannot silently override it.
        chosen = next((x for x in candidates if x["status"] == "FAIL"), None)
        chosen = chosen or next((x for x in candidates if x["status"] == "PASS"), None)
        chosen = chosen or (
            candidates[0]
            if candidates
            else dict(
                check=key,
                label=label,
                status="UNDEFINED",
                confirmed=False,
                source=None,
                quote=None,
                explanation="Политика не сохранена либо не идентифицирована; недостаток не установлен.",
            )
        )
        chosen["examined_sources"] = [p["url"] for p in policies]
        result.append(chosen)
    return result


REVIEW_CHECKS = {
    "vague_purposes": "Неконкретные цели обработки",
    "indefinite_retention": "Неопределённые сроки хранения",
    "advertising_consent": "Порядок согласия на рекламные сообщения",
    "third_party_conflict": "Противоречивые условия передачи данных",
    "health_processing": "Условия обработки сведений о здоровье",
}


def inspect_policy_review(packet, *, medical=False):
    """Narrow review observations in complete policies; never a verdict of illegality."""
    # shortcut: explicit clause combinations only; broaden after labelled policy review.
    rows = []
    for policy in policy_materials(packet):
        text = policy["text"]
        material = policy["material"]
        complete = policy["complete"] and material.http_status == 200
        observations = {}

        def find(pattern):
            return re.search(pattern, text, re.I)

        def sentence(match):
            start = max(text.rfind(char, 0, match.start()) for char in ".!?") + 1
            ends = [text.find(char, match.end()) for char in ".!?"]
            end = min((n for n in ends if n >= 0), default=len(text) - 1) + 1
            return text[start:end]

        if complete:
            # An open-ended catch-all is different from a specific goal expressed in synonyms.
            vague = find(
                r"(?:допускаются|возможны|предусмотрены)\s+иные\s+цели\s+обработки[^.!?]{0,300}"
            )
            if vague:
                observations["vague_purposes"] = (
                    [vague],
                    "Политика допускает иные, не названные здесь цели обработки. "
                    "Рекомендуется проверить их конкретизацию и связь с категориями данных; "
                    "упоминание согласия само по себе не раскрывает эти цели.",
                )

            # A general end-of-purpose clause only triggers together with an open-ended purpose.
            retention = find(
                r"(?:срок[а-яё]*\s+(?:хранения|обработки)[^.!?]{0,120}"
                r"(?:не\s+ограничен[а-яё]*|неопредел[её]н[а-яё]*))|"
                r"(?:обработка|хранение)\s+персональных\s+данных[^.!?]{0,100}без\s+ограничения\s+срока"
            )
            general_end = find(
                r"(?:хранени[ея]|хранится|хранятся|хранение)[^.!?]{0,180}"
                r"(?:не\s+дольше[^.!?]{0,70}цел[а-яё]*(?:\s+их)?\s+обработки|до\s+достижения\s+цел[а-яё]*\s+обработки)"
            )
            specific_end = find(
                r"(?:данные|сведения|обращени[яй]|заявк[аи])[^.!?]{0,10}хранятся\s+\d+\s*(?:дн|сут|месяц|лет|год)|"
                r"(?:хранятся|храним|сохраняем|хранения)[^.!?]{0,80}"
                r"(?:до\s+(?:завершения\s+(?:ответа|обращения)|исполнения\s+договора|отзыва\s+согласия)|"
                r"в\s+течение\s+\d+\s*(?:дн|сут|месяц|лет|год))"
            )
            if retention and not specific_end:
                observations["indefinite_retention"] = (
                    [retention],
                    "Документ прямо задаёт неопределённый или неограниченный срок обработки/хранения, "
                    "без найденного условия окончания для этих данных. Рекомендуется уточнить сроки "
                    "или понятные критерии по конкретным процессам; это не вывод о нарушении закона.",
                )
            elif vague and general_end and not specific_end:
                observations["indefinite_retention"] = (
                    [vague, general_end],
                    "Хранение связано с достижением целей, но политика одновременно допускает "
                    "неназванные иные цели. Для этой открытой группы целей окончание хранения "
                    "не конкретизировано. Рекомендуется проверить критерии по процессам; "
                    "сама формулировка «до достижения целей» недостатком не считается.",
                )

            advertising = find(
                r"(?:направлени[ея]|отправк[аи]|предоставлени[ея]|получени[ея])[^.!?;]{0,180}"
                r"рекламн[а-яё]*\s+(?:материал[а-яё]*|сообщени[а-яё]*|рассыл[а-яё]*)|"
                r"(?:рассылк[аи]|получени[ея])\s+рекламы"
            )
            ad_order = find(
                r"(?:отдельн[а-яё]*|самостоятельн[а-яё]*|предварительн[а-яё]*|специальн[а-яё]*)"
                r"[^.!?]{0,160}согласи[^.!?]{0,160}(?:реклам|рассыл)|"
                r"согласи[^.!?]{0,160}(?:реклам|рассыл)[^.!?]{0,160}"
                r"(?:отдельн|самостоятельн|предварительн|специальн)|"
                r"(?:реклам|рассыл)[^.!?]{0,160}(?:отдельн|предварительн|самостоятельн)"
                r"[^.!?]{0,120}согласи|"
                r"согласи[^.!?]{0,100}(?:отдельн|предварительн)[^.!?]{0,100}(?:реклам|рассыл)"
            )
            if ad_order and re.search(
                r"не\s+(?:требу|запраш|получа|оформля)", sentence(ad_order), re.I
            ):
                ad_order = None
            ad_document = any(
                re.search(r"согласи", link.text, re.I)
                and re.search(r"реклам|рассыл", link.text, re.I)
                for link in getattr(material, "relevant_links", [])
            )
            if (
                advertising
                and not ad_order
                and not ad_document
                and not re.search(
                    r"не\s+(?:направл|отправ|предоставл|получа)|"
                    r"не\s+означает\s+согласи|отказ[^.!?]{0,60}от\s+получ|только\s+при\s+наличии\s+отдельн",
                    sentence(advertising),
                    re.I,
                )
            ):
                observations["advertising_consent"] = (
                    [advertising],
                    "Рекламные материалы/сообщения включены в обработку данных, но в полном "
                    "тексте этой политики не описан самостоятельный порядок предварительного "
                    "согласия на рекламу. Рекомендуется проверить этот порядок и отдельные "
                    "документы; отсутствие согласия на всём сайте не установлено.",
                )

            denial = find(
                r"(?:персональн[а-яё]*\s+данн[а-яё]*|ПДн)[^.!?]{0,100}"
                r"(?:не\s+переда[а-яё]*|не\s+будут\s+переда[а-яё]*)"
                r"[^.!?]{0,100}(?:третьим|сторонним)\s+лицам"
            )
            transfer = find(
                r"(?:Оператор|Организация|Общество|Мы)\s+(?:может\s+)?(?:переда[а-яё]*|предоставля[а-яё]*)"
                r"[^.!?]{0,90}(?:персональн[а-яё]*\s+данн[а-яё]*|ПДн)"
                r"[^.!?]{0,120}(?:сервис[а-яё]*|партн[её]р[а-яё]*|подрядчик[а-яё]*|Метрик[а-яё]*)|"
                r"(?:персональн[а-яё]*\s+данн[а-яё]*|ПДн)\s+передаются"
                r"[^.!?]{0,120}(?:сервис[а-яё]*|партн[её]р[а-яё]*|подрядчик[а-яё]*|Метрик[а-яё]*)"
            )
            if (
                denial
                and transfer
                and not re.search(
                    r"исключени|кроме|без\s+согласия|если|случа[ея]|услови", sentence(denial), re.I
                )
                and not re.search(
                    r"не\s+переда|не\s+предоставля|обезличенн|анонимн", sentence(transfer), re.I
                )
            ):
                observations["third_party_conflict"] = (
                    [denial, transfer],
                    "В одном документе есть безусловный запрет передачи персональных данных "
                    "третьим лицам и прямое описание их передачи сервису/подрядчику. "
                    "Рекомендуется согласовать эти условия. Простое подключение сервиса "
                    "или передача только обезличенных данных основанием не являются.",
                )

            health = find(
                r"(?:клиент[а-яё]*|пациент[а-яё]*)[^.!?]{0,450}"
                r"(?:состояние\s+здоровья|диагноз[а-яё]*|жалоб[а-яё]*)\s*[,;]|"
                r"(?:собираем|обрабатываем|оператор\s+обрабатывает)\s+(?:сведения|данные)\s+"
                r"о\s+(?:состоянии\s+здоровья|диагнозе|жалобах)"
            )
            medical_purpose = find(
                r"(?:для|цел[ьях]+|обеспечение|организация)[^.!?]{0,100}"
                r"(?:оказани[ея]\s+медицинск[а-яё]*\s+(?:помощи|услуг)|диагностики|лечения)"
            )
            medical_basis = find(
                r"323\s*[-—–‑]?\s*ФЗ|врачебн[а-яё]*\s+тайн|"
                r"(?:здоровья|медицинск[а-яё]*\s+цел[а-яё]*)[^.!?]{0,180}"
                r"(?:письменн[а-яё]*\s+согласи|стать[яие]\s*10\b)|"
                r"письменн[а-яё]*\s+согласи[^.!?]{0,180}(?:здоровья|медицинск[а-яё]*\s+цел[а-яё]*)"
            )
            if (
                medical
                and health
                and not medical_purpose
                and not medical_basis
                and not re.search(
                    r"не\s+(?:собираем|обрабатываем|обрабатывает)", sentence(health), re.I
                )
            ):
                observations["health_processing"] = (
                    [health],
                    "В перечне фактически обрабатываемых данных названы сведения о здоровье, "
                    "но в полном тексте политики не найдена конкретизация медицинских целей "
                    "и специальных оснований их обработки. Рекомендуется проверить связь "
                    "категорий данных, целей и правовых оснований. Одно определение "
                    "специальных категорий данных таким признаком не считается.",
                )

        for key, label in REVIEW_CHECKS.items():
            observation = observations.get(key)
            quotes = []
            if observation:
                for match in observation[0]:
                    start, end = match.span()
                    # Keep exact original offsets even in PDF text containing line breaks.
                    end = min(end, start + 450)
                    quotes.append(dict(quote=text[start:end], start=start, end=end))
            rows.append(
                dict(
                    check=key,
                    label=label,
                    status="REVIEW"
                    if observation
                    else "NOT_TRIGGERED"
                    if complete
                    else "UNDEFINED",
                    confirmed=bool(observation),
                    manual_review=True,
                    legal_violation_established=False,
                    source=policy["url"],
                    evidence_id=material.evidence_id,
                    quotes=quotes,
                    quote=quotes[0]["quote"] if quotes else None,
                    explanation=observation[1]
                    if observation
                    else (
                        "Подтверждённого сочетания формулировок не найдено; соответствие закону не оценено."
                        if complete
                        else "Нет полного читаемого успешного ответа документа; вывод об отсутствии сведений запрещён."
                    ),
                    full_text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                    examined_chars=len(text) if complete else 0,
                    complete_readable_text=complete,
                    artifacts=[
                        dict(path=a.path, sha256=a.sha256) for a in policy["evidence"].artifacts
                    ],
                )
            )
    return rows
