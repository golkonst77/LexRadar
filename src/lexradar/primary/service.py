"""Deterministic internal structuring of integrity-checked saved evidence, never legal GO."""

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from ..auditors.evidence import load_packet
from ..collector.artifacts import verify_integrity
from ..collector.models import SCREENSHOT_NOTICE
from ..verifier.legal_cards import read_local
from ..verifier.registry import assess_registry, load_registry
from .markup import SavedMarkup, compact, link_kind
from .models import (
    AuditCoverage,
    CheckTask,
    Citation,
    CommercialOpportunity,
    FlowRow,
    FormRow,
    Hypothesis,
    MaterialReference,
    MedicalCheck,
    NormReference,
    Observation,
    OrganizationRow,
    PrimaryAudit,
    ServiceReference,
    StageResult,
)
from .stages import STAGES

ENTITY_NAMES = re.compile(
    r'(?:ООО|АО|ПАО|ЗАО|ГБУЗ|ГАУЗ|ИП)\s+[«"][^»"\n]{1,150}[»"]'
    r"|(?:ИП|Индивидуальный предприниматель)\s+[А-ЯЁ][а-яё-]+"
    r"(?:\s+[А-ЯЁ][а-яё-]+){1,2}"
)
MEDICAL_ITEMS = {
    "licensed_entity": ("лиценз",),
    "license_details": ("лиценз",),
    "organization_disclosures": ("ооо", "реквизит", "инн"),
    "services": ("услуг", "лечени"),
    "prices": ("цен", "стоимост", "прайс"),
    "medical_staff": ("врач", "специалист", "медицинск работник"),
    "paid_service_rules": ("правила предоставления", "правила оказания", "платных медицинских"),
    "other_applicable_disclosures": (),
}


def fingerprint(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def service_hint(reference: str) -> str | None:
    try:
        parsed = urlsplit(reference)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if host == "mc.yandex.ru":
        return "Yandex Metrika reference"
    if host in {"google-analytics.com", "www.google-analytics.com"}:
        return "Google Analytics reference"
    if host == "www.googletagmanager.com":
        return "Google Tag Manager reference; analytics execution not established"
    if host == "www.google.com" and parsed.path.startswith("/recaptcha/"):
        return "Google reCAPTCHA reference"
    if host == "api-maps.yandex.ru":
        return "Yandex Maps reference; not evidence of Metrika"
    return None


class Builder:
    def __init__(self, packet, root: Path, registry_path: Path | None, now: datetime):
        self.packet, self.root = packet, root
        self.evidence = {e.id: e for e in packet.data.evidence}
        self.stages = {s.stage_id: StageResult(stage_id=s.stage_id, title=s.title) for s in STAGES}
        self.observations: list[Observation] = []
        self.hypotheses: list[Hypothesis] = []
        self.tasks: list[CheckTask] = []
        self.work: list[CommercialOpportunity] = []
        self.texts: list[tuple[str, Citation]] = []
        self.markup: dict[str, SavedMarkup] = {}
        self.forms: list[FormRow] = []
        self.flows: list[FlowRow] = []
        self.services: list[ServiceReference] = []
        self.organizations: list[OrganizationRow] = []
        self.medical_checks: list[MedicalCheck] = []
        self.norms: list[NormReference] = []
        self.registry_hash = None
        self.notes = [
            "Рабочие этапы PR #10 не совпадают дословно с исходными этапами v2.5",
            "Обследованы сохранённые материалы, не живой сайт; полнота сайта неизвестна",
            "JavaScript, cookies, сетевые маршруты и функциональное поведение форм не проверялись",
            "Личность и роли организаций, нормативные основания и нарушения не подтверждены",
            "Упоминания имён/ИНН/ОГРН не устанавливают оператора или реального получателя данных",
            "Поля entities/forms из manifest не становятся фактами без повторного извлечения",
            "Изображения не осматривались; подписи не устанавливают личность изображённых",
            "РКН и реестры лицензий не запрашивались; недоступность не равна отсутствию записи",
            "Возможная юридическая работа требует проверки объёма, не доказывает нарушение",
        ]
        if registry_path is not None:
            registry, self.registry_hash = load_registry(registry_path)
            assessed = assess_registry(registry, packet.data.started_at.date(), now, 30)
            for source in [*registry.sources, *registry.cards]:
                self.norms.append(
                    NormReference(
                        id=source.id,
                        act_name=source.act_name,
                        act_number=source.act_number,
                        provision=source.provision,
                        quote=source.norm_text,
                        source_kind=getattr(source, "source_kind", "legacy_unclassified"),
                        assessment=assessed[source.id],
                    )
                )
        for definition in STAGES:
            stage = self.stages[definition.stage_id]
            stage.normative_reference_ids = [
                n.id for n in self.norms if n.assessment.domain in definition.domains
            ]
            stage.limitations = ["Нормативные ссылки — кандидаты, применимость не установлена"]
            stage.missing_information = ["Подтверждённая норма, редакция и применимость"]

    def cite(self, eid: str, basis: str, *, quote=None, page=None, element=None) -> Citation:
        evidence = self.evidence[eid]
        kind = "html" if eid in self.markup or basis == "saved_html" else "pdf"
        artifact = next((a for a in evidence.artifacts if a.kind == kind), None)
        if basis == "collector_manifest":
            artifact = None
        return Citation(
            evidence_id=eid,
            url=evidence.source,
            basis=basis,
            quote=quote,
            page=page,
            element=element,
            artifact_path=artifact.path if artifact else None,
            artifact_sha256=artifact.sha256 if artifact else None,
        )

    def observe(self, stage_id, kind, description, citations, scope) -> Observation:
        fact = Observation(
            id=f"observation-{len(self.observations) + 1:04d}",
            stage_id=stage_id,
            kind=kind,
            description=description,
            citations=citations,
            scope=scope,
        )
        self.observations.append(fact)
        stage = self.stages[stage_id]
        stage.observed_fact_ids.append(fact.id)
        stage.checked_materials = sorted(
            set(stage.checked_materials) | {c.evidence_id for c in citations}
        )
        if stage.status == "not_checked":
            stage.status = "observed"
        return fact

    def task(self, sid, kind, question, *, orgs=(), forms=(), evidence=(), external=False):
        task = CheckTask(
            id=f"check-{len(self.tasks) + 1:04d}",
            stage_id=sid,
            type=kind,
            question=question,
            organization_ids=list(orgs),
            form_ids=list(forms),
            evidence_ids=list(evidence),
            external_access_required=external,
        )
        self.tasks.append(task)
        self.stages[sid].additional_check_ids.append(task.id)
        return task.id

    def hypothesis(self, sid, question, facts, missing, checks):
        item = Hypothesis(
            id=f"hypothesis-{len(self.hypotheses) + 1:04d}",
            stage_id=sid,
            question=question,
            observation_ids=[f.id for f in facts],
            normative_reference_ids=self.stages[sid].normative_reference_ids,
            missing_information=missing,
            required_checks=checks,
        )
        self.hypotheses.append(item)
        self.stages[sid].hypothesis_ids.append(item.id)

    def opportunity(self, kind, sids, facts, rationale):
        self.work.append(
            CommercialOpportunity(
                id=f"work-{len(self.work) + 1:04d}",
                kind=kind,
                stage_ids=sids,
                observation_ids=[f.id for f in facts],
                rationale=rationale,
                prerequisites=[
                    "Независимая правовая оценка, актуальная норма и применимость",
                    "Определить необходимость и объём услуги после проверки фактов",
                ],
            )
        )
        for sid in sids:
            self.stages[sid].possible_paid_legal_work = True
            self.stages[sid].paid_work_rationale = rationale

    def load_materials(self):
        for page in self.packet.data.pages:
            evidence = self.evidence[page.evidence_id]
            artifact = next((a for a in evidence.artifacts if a.kind == "html"), None)
            if artifact is not None:
                raw = read_local(self.root / artifact.path, 20_000_000)
                if hashlib.sha256(raw).hexdigest() != artifact.sha256:
                    raise ValueError("Original changed during primary inspection")
                parser = SavedMarkup(str(evidence.source))
                parser.feed(raw.decode("utf-8", errors="replace"))
                parser.close()
                self.markup[evidence.id] = parser
                self.notes.extend(f"{evidence.id}: {reason}" for reason in parser.limitations)
            if page.text and page.reading.provenance == "reproduced":
                self.texts.append((page.text, self.cite(evidence.id, "reproduced_text", page=1)))
        for document in self.packet.data.documents:
            if document.reading.provenance == "reproduced":
                for number, text in sorted(document.page_texts.items()):
                    self.texts.append(
                        (text, self.cite(document.evidence_id, "reproduced_text", page=number))
                    )

    def organization_matrix(self):
        mentions = {}
        for text, citation in self.texts:
            matches = list(ENTITY_NAMES.finditer(text))
            for index, match in enumerate(matches):
                end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
                block = text[match.start() : min(end, match.start() + 1000)]
                name = match.group()
                # A singleton pattern is a text mention, never checked identity/ownership.
                inns = set(re.findall(r"ИНН\s*[:№]?\s*(\d{12}|\d{10})(?!\d)", block))
                ogrns = set(re.findall(r"ОГРН(?:ИП)?\s*[:№]?\s*(\d{15}|\d{13})(?!\d)", block))
                entry = mentions.setdefault(name, {"inn": set(), "ogrn": set(), "sources": []})
                entry["inn"].update(inns)
                entry["ogrn"].update(ogrns)
                entry["sources"].append(citation.model_copy(update={"quote": block}))
        for name, value in sorted(mentions.items()):
            uncertain = [
                "Личность, принадлежность реквизитов и роль не проверены по первоисточникам",
                "Количество упоминаний не доказывает количество операторов",
            ]
            if len(value["inn"]) > 1 or len(value["ogrn"]) > 1:
                uncertain.append(
                    "Противоречивые реквизиты одного наименования; идентичность неизвестна"
                )
            row = OrganizationRow(
                id=f"organization-{len(self.organizations) + 1:04d}",
                name=name,
                inn=next(iter(value["inn"])) if len(value["inn"]) == 1 else None,
                ogrn=next(iter(value["ogrn"])) if len(value["ogrn"]) == 1 else None,
                identification_sources=value["sources"],
                uncertainties=uncertain,
            )
            self.organizations.append(row)
            self.observe(
                "02",
                "text_quote",
                "В доступном тексте обнаружено упоминание организации",
                row.identification_sources,
                "Именованные фрагменты доступного текста",
            )
            self.observe(
                "01",
                "text_quote",
                "В материалах сайта упомянута организация; оператор не установлен",
                row.identification_sources,
                "Идентификационные текстовые упоминания, не реестры",
            )
        self.task(
            "01",
            "operator_identity_review",
            "Установить владельца сайта и фактического оператора",
            orgs=[o.id for o in self.organizations],
            evidence=self.evidence,
        )
        if not self.organizations:
            self.stages["01"].missing_information.append("Оператор и организация не установлены")
            self.stages["02"].missing_information.append("Подтверждённые сведения о юрлицах и ИП")
        else:
            self.stages["01"].status = "observed"

    def form_matrix(self):
        for page in self.packet.data.pages:
            parser = self.markup.get(page.evidence_id)
            if parser is None:
                continue
            for saved in parser.forms:
                links = {kind: [] for kind in ("policy", "consent", "advertising")}
                for link in parser.links:
                    kind = link_kind(link.url, link.text)
                    if link.form_index == saved.index and kind in links:
                        links[kind].append(link.url)
                form = FormRow(
                    id=f"form-{len(self.forms) + 1:04d}",
                    evidence_id=page.evidence_id,
                    url=self.evidence[page.evidence_id].source,
                    element=f"form[occurrence={saved.index}]",
                    purpose_hint=saved.purpose,
                    fields=saved.fields,
                    policy_links=sorted(set(links["policy"])),
                    consent_links=sorted(set(links["consent"])),
                    advertising_links=sorted(set(links["advertising"])),
                    interface_elements=saved.elements,
                    submission_context=compact(saved.parts),
                    action_reference=saved.action,
                    unresolved_processing=[
                        "Правовое основание, цели, сроки и фактический получатель",
                        "Поведение интерфейса, валидация и передача не тестировались",
                        "Ссылки распознаны по маркерам; содержание требует проверки",
                    ],
                    citation=self.cite(
                        page.evidence_id, "saved_html", element=f"form[occurrence={saved.index}]"
                    ),
                )
                if saved.index <= len(page.forms):
                    declared = page.forms[saved.index - 1]
                    if [(f.tag, f.type, f.name) for f in declared.fields] == [
                        (f.tag, f.type, f.name) for f in form.fields
                    ]:
                        form.screenshot = declared.screenshot
                        if declared.screenshot is not None:
                            form.screenshot_scope = (
                                SCREENSHOT_NOTICE + "; association is Collector-declared"
                            )
                if form.screenshot is None:
                    form.unresolved_processing.append(
                        "Скриншот формы недоступен или связь не подтверждена"
                    )
                for org in self.organizations:
                    if org.name in form.submission_context:
                        form.organization_candidates.append(org.id)
                        org.form_ids.append(form.id)
                        org.role_hint = (
                            "Назван в тексте формы; оператор/получатель фактически не установлен"
                        )
                if form.organization_candidates:
                    form.recipient_hint = (
                        "В контексте формы названы организации; это не проверка маршрута"
                    )
                self.forms.append(form)
                flow = FlowRow(
                    id=f"flow-{len(self.flows) + 1:04d}",
                    form_id=form.id,
                    action_reference=form.action_reference,
                    organization_candidates=form.organization_candidates,
                )
                self.flows.append(flow)
                for org in self.organizations:
                    if org.id in flow.organization_candidates:
                        org.flow_ids.append(flow.id)
                fact = self.observe(
                    "05",
                    "static_markup",
                    f"Статическая разметка формы {form.id}",
                    [form.citation],
                    "Только сохранённые HTML-атрибуты; не live UI",
                )
                for sid in ("06", "09"):
                    copied = self.observe(
                        sid,
                        "static_markup",
                        f"Контекст ссылок/маршрутизации формы {form.id}",
                        [form.citation],
                        "Сохранённая форма, не поведение и не передача",
                    )
                    check = self.task(
                        sid,
                        "form_legal_and_routing_review",
                        "Установить основания, применимость согласий и фактическую модель",
                        orgs=form.organization_candidates,
                        forms=[form.id],
                        evidence=[form.evidence_id],
                        external=sid == "09",
                    )
                    self.hypothesis(
                        sid,
                        f"Какие основания и получатели относятся к {form.id}?",
                        [copied],
                        form.unresolved_processing,
                        [check],
                    )
                self.opportunity(
                    "additional_legal_expertise",
                    ["05", "06", "09"],
                    [fact],
                    f"Форма {form.id} имеет наблюдаемые элементы; возможна отдельная оценка "
                    "основания и распределения ролей, необходимость документов не установлена",
                )
                if re.search(
                    r"реклам|маркетинг|рассыл|подпис",
                    form.submission_context + " " + (form.purpose_hint or ""),
                    re.IGNORECASE,
                ):
                    ad = self.observe(
                        "07",
                        "static_markup",
                        "Маркер рекламы/рассылки в контексте формы",
                        [form.citation],
                        "Не подтверждает отправку маркетинговых сообщений",
                    )
                    check = self.task(
                        "07",
                        "advertising_consent_review",
                        "Проверить характер коммуникаций и необходимость отдельного согласия",
                        forms=[form.id],
                        evidence=[form.evidence_id],
                    )
                    self.hypothesis(
                        "07",
                        "Применимо ли отдельное согласие на рекламу?",
                        [ad],
                        ["Фактические коммуникации и правовое основание"],
                        [check],
                    )
                field_captions = " ".join(
                    (f.name or "") + " " + (f.label or "") for f in form.fields
                )
                if re.search(
                    r"диагноз|жалоб|симптом|diagnosis|symptom|health", field_captions, re.IGNORECASE
                ):
                    health = self.observe(
                        "10",
                        "static_markup",
                        "В именах/подписях полей есть медицинский маркер; значения не извлекались",
                        [form.citation],
                        "Маркер не устанавливает фактический состав специальных данных",
                    )
                    check = self.task(
                        "10",
                        "data_category_review",
                        "Установить состав данных и применимые основания, без отправки формы",
                        forms=[form.id],
                        evidence=[form.evidence_id],
                    )
                    self.hypothesis(
                        "10",
                        "Обрабатываются ли специальные категории через эту форму?",
                        [health],
                        ["Фактически запрашиваемые данные и правовое основание"],
                        [check],
                    )

    def scoped_search(self, sid, description):
        citations = [c for _, c in self.texts]
        if citations:
            self.observe(
                sid,
                "scoped_search",
                description,
                citations,
                "Поиск маркеров только в перечисленных доступных текстовых фрагментах",
            )
            self.stages[sid].status = "insufficient_evidence"

    def policy_and_consents(self):
        for sid, kind in (("04", "policy"), ("06", "consent")):
            candidates = set()
            for eid, parser in self.markup.items():
                for link in parser.links:
                    if link_kind(link.url, link.text) == kind:
                        candidates.add(link.url)
                        self.observe(
                            sid,
                            "static_markup",
                            "Сохранённая ссылка-кандидат на документ",
                            [self.cite(eid, "saved_html", element=f"a[href={link.url}]")],
                            "Метка/адрес ссылки не подтверждают содержание документа",
                        )
            matched_text = []
            for text, citation in self.texts:
                if (
                    link_kind(str(citation.url), text[:300]) == kind
                    or str(citation.url) in candidates
                ):
                    match = re.search(r"политик\w*|согласи\w*|privacy|consent", text, re.IGNORECASE)
                    quote = (
                        text[max(0, match.start() - 30) : match.end() + 100]
                        if match
                        else text[:120]
                    )
                    fact = self.observe(
                        sid,
                        "text_quote",
                        "Доступен текстовый фрагмент документа-кандидата",
                        [citation.model_copy(update={"quote": quote})],
                        "Фрагмент документа; правовое содержание и полнота не подтверждены",
                    )
                    matched_text.append(fact)
                    material = next(
                        p
                        for p in [*self.packet.data.pages, *self.packet.data.documents]
                        if p.evidence_id == citation.evidence_id
                    )
                    if not material.reading.text_coverage_complete:
                        self.stages[sid].status = "insufficient_evidence"
                        self.stages[sid].limitations.append(
                            "Фрагмент не подтверждает полный текст документа"
                        )
            for document in self.packet.data.documents:
                if (
                    str(document.requested_url) in candidates
                    or link_kind(str(document.requested_url), "") == kind
                ):
                    reading = document.reading
                    if not reading.text_coverage_complete:
                        self.observe(
                            sid,
                            "collection_metadata",
                            "Документ-кандидат сохранён/заявлен; полного доступного текста нет",
                            [self.cite(document.evidence_id, "collector_manifest")],
                            "Скан/непрочитанные страницы требуют проверки, не являются нарушением",
                        )
                        self.stages[sid].status = "insufficient_evidence"
            if not self.stages[sid].observed_fact_ids:
                self.scoped_search(
                    sid, "Маркеры документа не обнаружены в проверенных текстовых областях"
                )
            self.task(
                sid,
                "document_content_review",
                "Проверить реальные ссылки, доступный текст и нормы",
                evidence=self.stages[sid].checked_materials,
            )
            self.stages[sid].missing_information.append(
                "Содержание, непрочитанные страницы и применимость"
            )
            if matched_text:
                self.opportunity(
                    "additional_legal_expertise",
                    [sid],
                    matched_text,
                    "Есть конкретные фрагменты документа для оценки; необходимость "
                    "подготовки нового документа не установлена",
                )

    def resources_and_publications(self):
        for eid, parser in self.markup.items():
            for kind, reference in parser.resources:
                row = ServiceReference(
                    id=f"service-{len(self.services) + 1:04d}",
                    url_reference=reference,
                    reference_kind=kind,
                    service_hint=service_hint(reference),
                    citation=self.cite(eid, "saved_html", element=f"{kind}[src]"),
                )
                self.services.append(row)
                self.observe(
                    "08",
                    "static_markup",
                    "В сохранённой разметке есть ссылка на ресурс",
                    [row.citation],
                    "Статическая ссылка; cookies/выполнение/передача не проверены",
                )
                flow_fact = self.observe(
                    "09",
                    "static_markup",
                    "Сохранён URL ресурса; сетевой запрос и получатель не установлены",
                    [row.citation],
                    "Статический адрес не доказывает трансграничную передачу",
                )
                check = self.task(
                    "09",
                    "resource_route_review",
                    "Установить реальное выполнение, маршрут и получателя по ресурсу",
                    evidence=[eid],
                    external=True,
                )
                self.hypothesis(
                    "09",
                    "Происходит ли передача по этой ссылке и каковы её условия?",
                    [flow_fact],
                    ["Сетевой маршрут, данные и основание"],
                    [check],
                )
            for reference, alt in parser.images:
                if re.search(r"пациент|отзыв|до.?после|результат|patient", alt, re.IGNORECASE):
                    fact = self.observe(
                        "10",
                        "static_markup",
                        "HTML img имеет тематическую подпись; содержимое не осмотрено",
                        [self.cite(eid, "saved_html", element=f"img[src={reference},alt={alt}]")],
                        "Атрибут подписи не доказывает изображение пациента или наличие ПДн",
                    )
                    check = self.task(
                        "10",
                        "visual_and_permission_review",
                        "Осмотреть изображение и проверить основания публикации, если применимо",
                        evidence=[eid],
                    )
                    self.hypothesis(
                        "10",
                        "Есть ли идентифицируемое изображение и применимые основания?",
                        [fact],
                        ["Содержимое изображения, основания, внутренние согласия"],
                        [check],
                    )
        self.task(
            "08",
            "cookie_and_integration_review",
            "Проверить поведение сервисов и cookies при отдельно разрешённом исследовании",
            evidence=self.markup,
            external=True,
        )
        self.task(
            "09",
            "network_route_review",
            "Установить фактические маршруты, получателей и применимость трансграничных правил",
            forms=[f.id for f in self.forms],
            evidence=self.markup,
            external=True,
        )
        if not self.services:
            self.stages["08"].missing_information.append(
                "Нет достаточных данных о работающих сервисах"
            )

    def registry_tasks(self):
        self.stages["11"].status = "not_checked"
        self.stages["11"].limitations.append("Публичные реестры не запрашивались")
        for org in self.organizations:
            self.task(
                "11",
                "rkn_registry_review",
                "Проверить идентичность, запись РКН и сведения; "
                "установить применимость обязанности уведомления",
                orgs=[org.id],
                evidence=[c.evidence_id for c in org.identification_sources],
                external=True,
            )
        if not self.organizations:
            self.task(
                "11",
                "rkn_identity_prerequisite",
                "Сначала установить оператора и идентификаторы; РКН не проверен",
                external=True,
            )

    def medical(self, requested_sector):
        sector = requested_sector
        if sector == "unknown" and any(
            re.search(r"клиник|стоматолог|медицинск", text, re.IGNORECASE) for text, _ in self.texts
        ):
            sector = "medical"
        for kind, markers in MEDICAL_ITEMS.items():
            facts = []
            if sector == "medical":
                for text, citation in self.texts:
                    match = (
                        re.search(
                            r"\b(?:" + "|".join(map(re.escape, markers)) + ")", text, re.IGNORECASE
                        )
                        if markers
                        else None
                    )
                    if match:
                        quote = text[max(0, match.start() - 30) : match.end() + 100]
                        facts.append(
                            self.observe(
                                "12",
                                "text_quote",
                                f"Текстовый маркер медицинского раскрытия: {kind}",
                                [citation.model_copy(update={"quote": quote})],
                                "Не проверка лицензии, обязательного содержания или полного сайта",
                            )
                        )
            status = (
                "observed"
                if facts
                else "insufficient_evidence"
                if sector == "medical"
                else "not_checked"
            )
            if kind in {"licensed_entity", "license_details", "other_applicable_disclosures"}:
                status = "insufficient_evidence" if sector == "medical" else "not_checked"
            self.medical_checks.append(
                MedicalCheck(
                    check_id=kind,
                    status=status,
                    observation_ids=[f.id for f in facts],
                    limitation="Маркеры в ограниченных фрагментах; "
                    "исполнители, реквизиты лицензии, "
                    "обязательность и полнота независимо не проверены",
                )
            )
        if sector == "medical":
            self.stages["12"].status = "insufficient_evidence"
            self.task(
                "12",
                "medical_license_registry_review",
                "Сопоставить лицо, реквизиты лицензии, услуги и действующие обязанности раскрытия",
                orgs=[o.id for o in self.organizations],
                evidence=self.evidence,
                external=True,
            )
            facts = [o for o in self.observations if o.stage_id == "12"]
            if facts:
                self.opportunity(
                    "medical_disclosure_review",
                    ["12"],
                    facts,
                    "Медицинские текстовые маркеры позволяют определить предмет проверки; "
                    "необходимость доработки и её объём ещё не установлены",
                )
        return sector

    def summary(self):
        for evidence in self.evidence.values():
            self.observe(
                "03",
                "collection_metadata",
                "Зарегистрирована попытка сбора материала",
                [self.cite(evidence.id, "collector_manifest")],
                "Сохранённая запись Collector; не проверка текущей доступности URL",
            )
        self.stages["03"].status = "insufficient_evidence" if self.evidence else "not_checked"
        self.stages["03"].missing_information.append("Знаменатель всех страниц сайта неизвестен")
        if self.evidence:
            self.observe(
                "13",
                "collection_metadata",
                "Локальное структурирование выполнено; аудит неполон",
                [self.cite(eid, "collector_manifest") for eid in self.evidence],
                "Сводка сохранённого досье, не подтверждение юридического исследования",
            )
            self.stages["13"].status = "insufficient_evidence"
        self.task(
            "13",
            "independent_legal_review",
            "Проверить нормы, применимость и опровержения; первичные гипотезы не являются выводами",
            orgs=[o.id for o in self.organizations],
            evidence=self.evidence,
        )
        self.task(
            "13",
            "methodology_reconciliation",
            "Согласовать рабочие этапы с исходным v2.5, включая документальную готовность РКН",
        )
        for sid in ("05", "07", "10"):
            if not self.stages[sid].observed_fact_ids:
                self.stages[sid].missing_information.append(
                    "Недостаточно предметных материалов по направлению"
                )

    def material_limitations(self):
        """Carry extraction limits into each affected stage, not just global notes."""
        materials = {
            p.evidence_id: p for p in [*self.packet.data.pages, *self.packet.data.documents]
        }
        for stage in self.stages.values():
            for eid in stage.checked_materials:
                material = materials[eid]
                reasons = list(material.reading.limitations)
                if not material.reading.text_coverage_complete:
                    reasons.append("Полнота текстового извлечения не подтверждена")
                if eid in self.markup:
                    reasons.extend(self.markup[eid].limitations)
                for reason in reasons:
                    note = f"{eid}: {reason}"
                    if note not in stage.limitations:
                        stage.limitations.append(note)
                incomplete = not material.reading.text_coverage_complete or (
                    eid in self.markup and bool(self.markup[eid].limitations)
                )
                if incomplete and stage.status in {"observed", "no_issue_observed"}:
                    stage.status = "insufficient_evidence"


def audit_saved_dossier(
    root: Path,
    *,
    registry_path: Path | None = None,
    sector: str = "unknown",
    as_of: datetime | None = None,
    max_input_bytes: int = 2_000_000,
) -> PrimaryAudit:
    """No A/B input, provider, browser, form action or client release API is accepted."""
    if sector not in {"medical", "unknown"} or not 1000 <= max_input_bytes <= 20_000_000:
        raise ValueError("Unsupported primary configuration")
    now = as_of or datetime.now(UTC)
    if now.utcoffset() is None:
        raise ValueError("Aware assessment time required")
    root = root.resolve()
    packet = load_packet(root, max_input_bytes)
    builder = Builder(packet, root, registry_path, now)
    builder.load_materials()
    builder.organization_matrix()
    builder.form_matrix()
    builder.policy_and_consents()
    builder.resources_and_publications()
    builder.registry_tasks()
    sector = builder.medical(sector)
    builder.summary()
    builder.material_limitations()
    documents = packet.data.documents
    pages = packet.data.pages
    readings = [p.reading for p in [*pages, *documents]]
    inventory = [
        {"evidence_id": e.id, **artifact.model_dump(mode="json")}
        for e in packet.data.evidence
        for artifact in e.artifacts
    ]
    if (
        verify_integrity(packet.data, root)
        or hashlib.sha256(read_local(root / "collection.json", 20_000_000)).hexdigest()
        != packet.manifest_sha256
    ):
        raise ValueError("Dossier changed during primary inspection")
    return PrimaryAudit(
        target_url=packet.data.target_url,
        dossier_sha256=packet.manifest_sha256,
        artifact_inventory_sha256=fingerprint(inventory),
        source_registry_sha256=builder.registry_hash,
        source_started_at=packet.data.started_at,
        assessed_at=now,
        sector_hint=sector,
        organizations=builder.organizations,
        operator_situation=(
            "operator_unestablished"
            if not builder.organizations
            else "one_organization_mentioned"
            if len(builder.organizations) == 1
            else "multiple_roles_unestablished"
        ),
        materials=[
            MaterialReference(
                evidence_id=item.evidence_id,
                url=builder.evidence[item.evidence_id].source,
                collection_status=builder.evidence[item.evidence_id].status,
                reading=item.reading,
                artifacts=builder.evidence[item.evidence_id].artifacts,
            )
            for item in [*pages, *documents]
        ],
        forms=builder.forms,
        flows=builder.flows,
        services=builder.services,
        medical_checks=builder.medical_checks,
        stages=list(builder.stages.values()),
        observations=builder.observations,
        hypotheses=builder.hypotheses,
        normative_references=builder.norms,
        missing_checks=builder.tasks,
        commercial_opportunity=builder.work,
        coverage=AuditCoverage(
            page_attempts=len(pages),
            document_attempts=len(documents),
            reproduced_text_materials=sum(r.provenance == "reproduced" for r in readings),
            incomplete_text_materials=sum(not r.text_coverage_complete for r in readings),
            known_pdf_pages=sum(d.reading.page_count or 0 for d in documents),
            supplied_pdf_text_pages=sum(len(d.page_texts) for d in documents),
            unknown_pdf_page_count_documents=sum(d.reading.page_count is None for d in documents),
            stages_with_observations=sum(
                bool(s.observed_fact_ids) for s in builder.stages.values()
            ),
        ),
        limitations=[*packet.limitations, *builder.notes],
    )
