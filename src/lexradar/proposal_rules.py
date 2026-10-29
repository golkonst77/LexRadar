"""Fixed client copy and conservative triggers from integrity-checked saved evidence."""

import re
from urllib.parse import urlsplit

# Approved verbatim in Codex_LexRadar_5_rules_production_2026-10-27.txt.
BLOCKS = {
    1: {
        "title": "НЕДОСТАТКИ ПОЛИТИКИ ОБРАБОТКИ ПЕРСОНАЛЬНЫХ ДАННЫХ",
        "paragraphs": [
            (
                "При обследовании сайта выявлены недостатки в размещении или содержании "
                "политики обработки персональных данных, в том числе возможные расхождения "
                "между целями обработки, указанными в политике, сведениями реестра "
                "Роскомнадзора и фактическими процессами организации."
            ),
            (
                "Отсутствие политики, её несоответствие требованиям законодательства или "
                "недостоверность сведений, представленных в Роскомнадзор, могут повлечь "
                "административную ответственность по ст. 13.11 КоАП РФ. (Штраф 300 000 – 700 "
                "000 р.)"
            ),
        ],
    },
    2: {
        "title": "НЕДОСТАТКИ ФОРМ ЗАПИСИ И ОБРАТНОЙ СВЯЗИ",
        "paragraphs": [
            (
                "При обследовании сайта выявлены недостатки в формах записи и обратной связи, "
                "через которые посетители передают свои персональные данные. В частности, "
                "отсутствует необходимая информация об обработке данных либо некорректно "
                "оформлены ссылки на соответствующие документы."
            ),
            (
                "Нарушение требований к сбору персональных данных и информированию "
                "пользователей может повлечь административную ответственность по ст. 13.11 КоАП "
                "РФ. (Штраф 300 000 – 700 000 р.)"
            ),
        ],
    },
    3: {
        "title": "НАРУШЕНИЯ ПОРЯДКА ПОЛУЧЕНИЯ СОГЛАСИЯ НА ОБРАБОТКУ ПЕРСОНАЛЬНЫХ ДАННЫХ",
        "paragraphs": [
            (
                "При обследовании сайта выявлены недостатки в механизме получения согласия на "
                "обработку персональных данных: отсутствие необходимого согласия, некорректные "
                "формулировки либо использование заранее установленных отметок."
            ),
            (
                "Ненадлежащее получение или оформление согласия, когда оно необходимо по "
                "закону, может повлечь административную ответственность по ст. 13.11 КоАП РФ. "
                "(Штраф 300 000 – 700 000 р.)"
            ),
        ],
    },
    4: {
        "title": "НАРУШЕНИЯ ПРИ ИСПОЛЬЗОВАНИИ COOKIE И СЕРВИСОВ ВЕБ-АНАЛИТИКИ",
        "paragraphs": [
            (
                "При обследовании сайта выявлены недостатки в использовании файлов cookie и "
                "сервисов веб-аналитики: отсутствует необходимое информирование посетителей, "
                "некорректно оформлено согласие либо фактическая обработка данных не "
                "соответствует опубликованным документам."
            ),
            (
                "Нарушение требований законодательства при сборе и обработке персональных "
                "данных посетителей сайта может повлечь административную ответственность по ст. "
                "13.11 КоАП РФ. (Штраф 300 000 – 700 000 р.)"
            ),
        ],
    },
    5: {
        "title": "ПРОТИВОРЕЧИЯ И НЕДОСТОВЕРНЫЕ СВЕДЕНИЯ В ЮРИДИЧЕСКИХ ДОКУМЕНТАХ",
        "paragraphs": [
            (
                "При обследовании сайта выявлены противоречия в опубликованных юридических "
                "документах: расхождения в наименовании организации, реквизитах, сведениях об "
                "операторе персональных данных либо условиях обработки персональных данных."
            ),
            (
                "Недостоверные или противоречивые сведения в обязательных документах могут "
                "свидетельствовать о нарушении требований законодательства и повлечь "
                "административную ответственность по ст. 13.11 КоАП РФ. (Штраф 300 000 – 700 "
                "000 р.)"
            ),
        ],
    },
}


HEALTH_FIELD_BLOCK = {
    "title": "ВОЗМОЖНЫЙ СБОР СВЕДЕНИЙ О СОСТОЯНИИ ЗДОРОВЬЯ",
    "paragraphs": [
        "На сайте предусмотрено поле свободного ввода сведений о причинах обращения, "
        "жалобах или состоянии здоровья пациента. Такие сведения могут относиться к "
        "специальным категориям персональных данных. Рекомендуется проверить правовые "
        "основания их обработки и соответствие опубликованных документов фактическому сбору данных"
    ],
}


class NoProposalTriggers(ValueError):
    """No confirmed deficiency: do not create a client proposal."""


def document_kind(label):
    label = label.casefold()
    if (
        re.search(r"\bполитик[а-яё]*\b", label)
        and re.search(r"конфиденц|персональн|обработк|\bпдн?\b", label)
        or re.search(r"\bprivacy\s+policy\b", label)
    ):
        return "policy"
    if re.search(r"согласи[а-яё]*", label) and re.search(
        r"персональн|обработк.{0,30}данн|\bпдн?\b", label
    ):
        return "consent"
    return None


def personal_fields(form):
    return [
        field
        for field in form.fields
        if field.type in {"tel", "email"}
        or (
            field.type in {"text", "textarea"}
            and re.search(
                r"имя|фио|телефон|почт|name|phone|email",
                " ".join((field.name or "", field.label or "")),
                re.I,
            )
        )
    ]


def inspect_form_signals(packet):
    """Facts from saved forms; suspicions never establish a legal violation."""
    if packet is None:
        return []
    signals = []
    evidence = {item.id: item for item in packet.data.evidence}

    def emit(page, signal, status, quote, reason, pointer, **extra):
        signals.append(
            dict(
                signal=signal,
                status=status,
                quote=quote,
                reason=reason,
                url=str(page.final_url or page.requested_url),
                evidence_id=page.evidence_id,
                manifest_pointer=pointer,
                artifacts=[
                    dict(path=a.path, sha256=a.sha256) for a in evidence[page.evidence_id].artifacts
                ],
                legal_violation_established=False,
                **extra,
            )
        )

    for page in packet.data.pages:
        if not evidence[page.evidence_id].available:
            continue
        for index, form in enumerate(page.forms):
            context = form.submission_context or ""
            pointer = f"forms/{index}"
            fields = personal_fields(form)
            # shortcut: unlabelled generic controls remain uncertain; do not infer their purpose.
            purpose = re.search(
                r"запис[а-яё]*|обратн[а-яё]* звон[а-яё]*|оставить заявку", context, re.I
            )
            if not fields and not (
                purpose
                and any(
                    f.type not in {"hidden", "submit", "button", "checkbox", "radio"}
                    for f in form.fields
                )
            ):
                continue
            links = [*form.policy_links, *form.consent_links]
            info = re.search(
                r"персональн[а-яё]* данн[а-яё]*|обработк[а-яё]* данн[а-яё]*|политик[а-яё]* конфиденциальности",
                context,
                re.I,
            )
            emit(
                page,
                "form_information",
                "НЕ ОБНАРУЖЕНО" if links or info else "НА РУЧНУЮ ПРОВЕРКУ",
                info[0]
                if info
                else (
                    context[:220] or (fields[0].name or fields[0].type) if fields else context[:220]
                ),
                "Информация или ссылка при форме найдена; корректность документа требует отдельной проверки"
                if links or info
                else "В сохранённой форме не найдена привязанная информация; другие основания обработки и соседний HTML не исключены",
                pointer,
                links=links,
                personal_fields=len(fields),
                purpose=purpose[0] if purpose else form.purpose,
            )
            contradiction = re.search(
                r"(?:не собираем|не обрабатываем|не осуществляется обработка)\s+персональн[а-яё]*\s+данн[а-яё]*",
                context,
                re.I,
            )
            if fields and contradiction:
                emit(
                    page,
                    "form_information_contradiction",
                    "НА РУЧНУЮ ПРОВЕРКУ",
                    contradiction[0],
                    "Текст отрицает обработку при наличии персональных полей; отправка и контекст утверждения не проверены",
                    pointer,
                )
            controls = [f for f in form.fields if f.type in {"checkbox", "radio"}]
            for number, field in enumerate(form.fields):
                if field.type not in {"checkbox", "radio"}:
                    continue
                label = field.label or ""
                kind = (
                    "cookie"
                    if re.search(r"cookies?|куки", label, re.I)
                    else "advertising"
                    if re.search(r"реклам|рассыл|маркетинг", label, re.I)
                    else "personal_data"
                    if re.search(r"обработк[а-яё]*.{0,35}данных", label, re.I)
                    else "policy_acknowledgment"
                    if re.search(r"политик|конфиденц", label, re.I)
                    else "unresolved"
                )
                status = (
                    "ПОДТВЕРЖДЕНО"
                    if field.checked is True and label
                    else "НА РУЧНУЮ ПРОВЕРКУ"
                    if not label
                    else "НЕ ОБНАРУЖЕНО"
                )
                emit(
                    page,
                    "preset_consent",
                    status,
                    label[:300],
                    "Исходная отметка подтверждена; назначение, активное волеизъявление и правовое основание проверить вручную"
                    if field.checked is True
                    else "Исходная отметка не обнаружена"
                    if label
                    else "Нет связанного текста элемента; текст формы не приписывается произвольному чекбоксу",
                    f"{pointer}/fields/{number}",
                    consent_kind=kind,
                    checked=field.checked,
                    links=links,
                    manual_review=field.checked is True or not label,
                )
            if not controls and info:
                emit(
                    page,
                    "consent_text_without_checkbox",
                    "НА РУЧНУЮ ПРОВЕРКУ",
                    info[0],
                    "Есть текст информирования; отсутствие чекбокса само по себе не нарушение",
                    pointer,
                )
        if page.cookie_text_observed:
            text = page.text or ""
            continued = re.search(
                r"продолжая[^.!?]{0,280}(?:соглаша[а-яё]*|согласие)[^.!?]{0,100}", text, re.I
            )
            if continued:
                emit(
                    page,
                    "cookie_continued_use" if re.search(r"cookies?|куки", continued[0], re.I)
                    else "continued_use_notice",
                    "ПОДТВЕРЖДЕНО",
                    continued[0],
                    "Текст продолжения использования найден; назначение cookie и основание обработки не установлены",
                    "text",
                    consent_kind=("cookie" if re.search(r"cookies?|куки", continued[0], re.I)
                                  else "unresolved"),
                    manual_review=True,
                )
            controls = getattr(page, "consent_management_controls", [])
            if not any(
                re.search(r"отказ|отклон|reject|не соглас|только необходим", c, re.I)
                for c in controls
            ):
                emit(
                    page,
                    "cookie_refusal_not_observed",
                    "НА РУЧНУЮ ПРОВЕРКУ",
                    continued[0] if continued else "",
                    "В сохранённых элементах не найден отказ; недоступность отказа при работе сайта не доказана",
                    "consent_management_controls",
                    consent_kind="cookie",
                )
    if not any(s["signal"] == "form_information" for s in signals):
        signals.append(
            dict(
                signal="form_extraction",
                status="НА РУЧНУЮ ПРОВЕРКУ",
                extraction_status="НЕОПРЕДЕЛЕНО",
                quote="",
                url=str(getattr(packet.data, "target_url", "")),
                reason="Форма с персональными полями не извлечена; это не доказательство отсутствия формы или нарушения",
                legal_violation_established=False,
            )
        )
    return signals


def evaluate_rules(packet):
    """A positive feature or missing evidence never establishes a deficiency."""
    if packet is None:
        return []
    evidence = {item.id: item for item in packet.data.evidence}
    pages = [page for page in packet.data.pages if evidence[page.evidence_id].available]
    failures = {
        str(page.requested_url): page
        for page in packet.data.pages
        if page.http_status in {404, 410}
    }
    rules = {}

    def citation(page, pointer, quote, checked):
        return dict(
            evidence_id=page.evidence_id,
            url=str(page.final_url or page.requested_url),
            manifest_pointer=pointer,
            quote=quote,
            checked=checked,
            artifacts=[
                dict(path=a.path, sha256=a.sha256) for a in evidence[page.evidence_id].artifacts
            ],
            scope="Сохранённые данные; текущая работа сайта и JavaScript не проверялись",
        )

    def add(number, proof):
        if number not in rules:
            rules[number] = dict(rule_id=number, confirmed=True, evidence=[proof])
            if number in {2, 3}:
                rules[number].update(manual_review=True, legal_violation_established=False)

    medical = any(
        re.search(r"\bклиник[а-яё]*|стоматолог|медицинск[а-яё]*\s+(?:центр|клиник|организац|услуг)|офтальмолог|центр.{0,30}зрени|больниц|поликлиник|медцентр",
                  page.title or "", re.I)
        for page in pages
    ) and not any(re.search(r"ветеринар|ветклиник", page.title or "", re.I) for page in pages)
    if medical:
        for page in pages:
            if page.http_status != 200 or page.reading.provenance != "reproduced":
                continue
            for form_index, form in enumerate(page.forms):
                for field_index, field in enumerate(form.fields):
                    if (field.disabled or field.readonly or field.tag not in {"textarea", "input"}
                        or field.type not in {"text", "textarea"}):
                        continue
                    if re.fullmatch(r"name|first_name|last_name|surname|fio|phone|tel|email|e-mail",
                                    field.name or "", re.I):
                        continue
                    description = " ".join(filter(None, (
                        field.label, field.placeholder, field.aria_label, field.associated_label,
                    )))
                    if re.search(
                        r"причин[аы]\s+(?:вашего\s+)?обращения|жалоб|симптом|опишите\s+(?:вашу\s+)?проблем|"
                        r"что\s+вас\s+беспокоит|описание\s+заболевания|диагноз|самочувств",
                        description, re.I,
                    ):
                        proof = citation(
                            page, f"forms/{form_index}/fields/{field_index}", description,
                            HEALTH_FIELD_BLOCK["paragraphs"][0],
                        )
                        proof["field"] = field.model_dump()
                        proof["medical_context"] = [p.title for p in pages if p.title]
                        proof["legal_violation_established"] = False
                        add(6, proof)
                        rules[6].update(manual_review=True, legal_violation_established=False)

    for page in pages:
        for form_index, form in enumerate(page.forms):
            if not personal_fields(form):
                continue
            for link in [*form.policy_links, *form.consent_links]:
                if link in failures:
                    failed = failures[link]
                    proof = citation(
                        page,
                        f"forms/{form_index}",
                        link,
                        f"У формы с персональными полями ссылка на документ {link} "
                        f"имеет сохранённый ответ HTTP {failed.http_status}",
                    )
                    proof["response"] = citation(
                        failed,
                        "http_status",
                        str(failed.http_status),
                        "Сохранённый статус ответа на адрес документа",
                    )
                    add(2, proof)
            # Positive document identity is usable even when its unread tail is withheld.
            if re.search(
                r"в\s+Согласии\s+на\s+обработку\s+персональных\s+данных",
                form.submission_context, re.I,
            ):
                for link in page.relevant_links:
                    if not (
                        link.visible_in_static_html
                        and link.url in form.consent_links
                        and re.fullmatch(r"\s*согласие\s*", link.text, re.I)
                    ):
                        continue
                    for document in pages:
                        declaration = re.search(
                            r"Настоящая\s+Политика\s+(?:конфиденциальности|обработки)"
                            r"[^.!?]{0,120}", document.text or "", re.I,
                        )
                        if (
                            link.url not in {str(document.requested_url), str(document.final_url)}
                            or document.http_status != 200
                            or page.http_status != 200
                            or document.reading.provenance != "reproduced"
                            or page.reading.provenance != "reproduced"
                            or not re.search(r"Политика\s+(?:конфиденциальности|обработки)",
                                             document.title or "", re.I)
                            or not declaration
                        ):
                            continue
                        proof = citation(
                            page, f"forms/{form_index}", form.submission_context,
                            "Форма отсылает к Согласию на обработку персональных данных, "
                            "но ссылка «согласие» ведёт на документ, явно названный политикой; "
                            "подтверждено несоответствие назначения ссылки и документа, "
                            "а не отсутствие согласия или установленное нарушение закона",
                        )
                        proof["form_check"] = "consent_link_targets_policy"
                        proof["document"] = citation(
                            document, "text", declaration[0],
                            "Название и начало сохранённого текста прямо определяют документ как политику",
                        )
                        add(2, proof)
            for field_index, field in enumerate(form.fields):
                label = field.label or ""
                consent = re.search(
                    r"(?:Я\s+)?(?:ознакомлен\s+и\s+)?соглас(?:ен|на|ие)\s+"
                    r"(?:с\s+условиями\s+|на\s+)?обработк[а-яё]*\s+"
                    r"(?:моих\s+)?(?:персональных\s+)?данных",
                    label,
                    re.I,
                )
                linked_consent = None
                if (not consent and field.type == "checkbox" and field.checked is True
                    and document_kind(label) == "policy"):
                    linked_consent = next((
                        document for document in pages
                        if document.http_status == 200
                        and document.reading.provenance == "reproduced"
                        and str(document.requested_url) in form.policy_links
                        and document_kind(document.title or "") == "consent"
                        and re.search(r"выражаю\s+свое\s+(?:безусловное\s+)?согласие\s+"
                                      r"на\s+обработку\s+моих\s+персональных\s+данных",
                                      document.text or "", re.I)
                    ), None)
                    if linked_consent is not None:
                        consent = re.search(r".+", label, re.S)
                if field.type == "checkbox" and field.checked is True and consent:
                    proof = citation(
                        page,
                        f"forms/{form_index}/fields/{field_index}",
                        consent[0],
                        f"Форма {form_index + 1}, поле {field.name or field_index + 1}: "
                        "в сохранённом наблюдении type=checkbox, checked=true. "
                        "Проверена исходная отметка согласия на обработку данных; "
                        "фактическая отправка и применимость ответственности не проверялись",
                    )
                    if linked_consent is not None:
                        declaration = re.search(
                            r"выражаю\s+свое\s+(?:безусловное\s+)?согласие\s+на\s+обработку\s+"
                            r"моих\s+персональных\s+данных", linked_consent.text, re.I,
                        )
                        proof["document"] = citation(
                            linked_consent, "text", declaration[0],
                            "Подпись галочки называет политику, но связанный документ явно является согласием",
                        )
                    proof["field"] = dict(type=field.type, name=field.name, checked=True)
                    proof["status"] = "ПОДТВЕРЖДЕНО"
                    proof["manual_review"] = True
                    proof["legal_violation_established"] = False
                    add(3, proof)

    from .policy5 import inspect_policy5, policy_materials

    materials = [*packet.data.pages, *getattr(packet.data, "documents", [])]
    # Missing or unread material cannot prove that a policy was not found.
    search_complete = bool(pages) and all(
        evidence[m.evidence_id].available
        and m.http_status is not None
        and 200 <= m.http_status < 300
        and bool((m.text or "").strip())
        and m.reading.provenance == "reproduced"
        and m.reading.text_coverage_complete
        and m.reading.text_truncated is False
        for m in materials
    )
    policy_link_found = any(
        document_kind(link.text) == "policy" or link.kind == "privacy_policy"
        for page in pages for link in page.relevant_links
    ) or any(form.policy_links for page in pages for form in page.forms)
    if search_complete and not policy_link_found and not policy_materials(packet):
        for page in pages:
            for form_index, form in enumerate(page.forms):
                fields = personal_fields(form)
                if fields:
                    proof = citation(
                        page, f"forms/{form_index}",
                        ", ".join(field.label or field.name or field.type for field in fields),
                        "Обнаружена форма сбора персональных данных; политика не найдена "
                        "после проверки полного сохранённого текста доступных страниц и документов",
                    )
                    proof["policy_check"] = "not_found_on_checked_pages"
                    proof["examined_sources"] = [
                        str(m.final_url or m.requested_url) for m in materials
                    ]
                    add(1, proof)


    policies = policy_materials(packet)
    policy_urls = {p["url"].split("#")[0].rstrip("/") for p in policies}
    main_pages = [
        p for p in packet.data.pages
        if str(p.final_url or p.requested_url).split("#")[0].rstrip("/") not in policy_urls
    ]
    main_pages_complete = bool(main_pages) and any(
        urlsplit(str(p.final_url or p.requested_url)).path in {"", "/"}
        for p in main_pages
    ) and all(
        evidence[p.evidence_id].available
        and p.http_status is not None and 200 <= p.http_status < 300
        and bool((p.text or "").strip())
        and p.reading.provenance == "reproduced"
        and p.reading.text_coverage_complete and p.reading.text_truncated is False
        for p in main_pages
    ) and not any(
        re.search(r"stopped|limit|failed|error|incomplete|недоступ|неполн|лимит", warning, re.I)
        for warning in getattr(packet.data, "warnings", [])
    )
    visitor_link_found = any(
        link.visible_in_static_html and (
            document_kind(link.text) == "policy" or link.kind == "privacy_policy"
            or link.url.split("#")[0].rstrip("/") in policy_urls
        )
        for page in main_pages for link in page.relevant_links
    ) or any(form.policy_links for page in main_pages for form in page.forms)
    policy_exists = any(
        evidence[p["material"].evidence_id].available
        and p["material"].http_status is not None
        and 200 <= p["material"].http_status < 300
        and bool(p["text"].strip())
        for p in policies
    )
    if policy_exists and main_pages_complete and not visitor_link_found:
        for page in main_pages:
            for index, form in enumerate(page.forms):
                if personal_fields(form):
                    proof = citation(
                        page, f"forms/{index}", "",
                        "Документ политики технически существует, но на полностью прочитанных "
                        "основных страницах пользовательские ссылки на него не найдены; "
                        "выявлен признак затруднённого доступа к политике",
                    )
                    proof.update(
                        policy_check="policy_access_link_not_found",
                        policy_document_exists=True, visitor_policy_link_found=False,
                        policy_sources=sorted(policy_urls),
                        examined_sources=[str(p.final_url or p.requested_url) for p in main_pages],
                    )
                    add(1, proof)

    for check in inspect_policy5(packet):
        if check["status"] == "FAIL" and check["confirmed"]:
            add(
                1,
                dict(
                    evidence_id=check["evidence_id"],
                    url=check["source"],
                    quote=check["quote"],
                    checked=check["explanation"],
                    policy_check=check["check"],
                    policy_operator=check.get("policy_operator"),
                    document_scope=check.get("document_scope"),
                    page=check["page"],
                    full_text_sha256=check["full_text_sha256"],
                    examined_chars=check["examined_chars"],
                    complete_readable_text=True,
                    artifacts=check["artifacts"],
                    scope="Полный сохранённый текст политики; внешняя юридическая квалификация не выполнялась",
                ),
            )

    # shortcut: only explicit operator declarations with the same OGRN are compared;
    # widen this after independent document review, never compare incidental third parties.
    operators = {}
    pattern = re.compile(
        r"Оператор\s+персональных\s+данных\s*[:—-]\s*"
        r"(?P<name>ООО\s*[«\"][^»\"]{1,120}[»\"])\s*[,;]?\s*"
        r"ИНН\s*:?\s*(?P<inn>\d{10}|\d{12})(?!\d)\s*[,;]?\s*"
        r"ОГРН\s*:?\s*(?P<ogrn>\d{13})(?!\d)",
        re.I,
    )
    document_urls = {
        link.url
        for page in pages
        for link in page.relevant_links
        if link.visible_in_static_html and document_kind(link.text)
    }
    for page in pages:
        if str(page.final_url or page.requested_url) not in document_urls:
            continue
        for match in pattern.finditer(page.text or ""):
            previous = operators.get(match["ogrn"])
            if (
                previous
                and previous[1]["inn"] != match["inn"]
                and previous[0].evidence_id != page.evidence_id
            ):
                proof = citation(
                    page,
                    "text",
                    match[0],
                    "В двух явных декларациях оператора с одним ОГРН указаны разные ИНН; "
                    "сверка с реестром РКН не выполнялась",
                )
                proof["compared_with"] = citation(
                    previous[0], "text", previous[1][0], "Вторая явная декларация того же ОГРН"
                )
                add(5, proof)
            else:
                operators[match["ogrn"]] = (page, match)
    from .policy5 import inspect_policy_review

    review_signals = [row for row in inspect_policy_review(packet, medical=medical)
                      if row["confirmed"] and row["complete_readable_text"]]
    if review_signals:
        rules[7] = dict(rule_id=7, confirmed=True, manual_review=True,
                        legal_violation_established=False, evidence=review_signals)
    return [rules[number] for number in sorted(rules)]
