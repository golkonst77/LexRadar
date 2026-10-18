"""Deterministic A–H qualification of saved primary observations, never legal confirmation."""

import hashlib
import json
import re

from .models import PrimaryAudit
from .qualification_models import (
    SUMMARY_REASONS,
    CommercialWorkCandidate,
    FindingFact,
    InternalAssessment,
    LegalFinding,
    PreliminaryClientSummary,
    ReviewObject,
    SummaryDirection,
)

TOPICS = {
    "A": "Идентификация фактического оператора персональных данных",
    "B": "Сопоставление политики с формами и фактическими целями",
    "C": "Основания обработки и связь согласия с конкретной формой",
    "D": "Применимость отдельного согласия на рекламные коммуникации",
    "E": "Роли третьих лиц во внешней записи",
    "F": "Фактическое поведение аналитических инструментов",
    "G": "Применимость и содержание медицинских раскрытий",
    "H": "Разграничение ролей отдельно упомянутых организаций",
}
CHECKS = {
    "A": [
        "Независимо установить личность и фактическую роль каждой организации",
        "Раздельно установить владельца сайта, домена и оператора ПДн",
    ],
    "B": [
        "Получить полный действующий текст политики, включая непрочитанные страницы",
        "Сопоставить подтверждённые цели, категории данных и роли с конкретной формой",
    ],
    "C": [
        "Установить применимое основание обработки; согласие не предполагается обязательным",
        "Проверить текст, интерфейс и доказательства получения согласия, если оно применимо",
    ],
    "D": [
        "Подтвердить фактические рекламные коммуникации и применимые актуальные нормы",
        "Отдельно проверить основания и получение рекламного согласия, если требуется",
    ],
    "E": [
        "Установить, работает ли внешняя запись и какие данные фактически передаются",
        "Проверить роль получателя, договоры и применимые основания",
    ],
    "F": [
        "Отдельно разрешённой проверкой установить исполнение, cookies и маршруты",
        "Проверить получателей, данные и применимые актуальные нормы",
    ],
    "G": [
        "Подтвердить исполнителя, реквизиты и статус медицинской лицензии",
        "Исследовать полный сайт и документы; установить действующие обязанности раскрытия",
    ],
    "H": [
        "Проверить контекст каждого упоминания, не предполагая нескольких операторов",
        "Установить независимые роли и фактические связи организаций с формами",
    ],
}
UNKNOWN = {
    "A": ["Упоминание не устанавливает идентичность, собственника или оператора"],
    "B": ["Полнота, применимость документа и его соответствие фактической обработке неизвестны"],
    "C": ["Наличие/отсутствие checkbox не устанавливает правомерность обработки"],
    "D": ["Маркер рекламы не устанавливает факт рассылки или обязательность согласия"],
    "E": ["Ссылка или iframe не доказывают передачу и не устанавливают получателя"],
    "F": ["GTM не доказывает GA; декларация ресурса не доказывает cookies или передачу"],
    "G": ["Текстовый маркер не подтверждает лицензию или исполнение всех обязанностей"],
    "H": ["Разные упоминания могут относиться к биографии/партнёрам, а не операторам сайта"],
}
WORK_RESULTS = {
    "policy_document": (
        "Подготовка или переработка политики с учётом подтверждённой модели обработки"
    ),
    "processing_consent": "Подготовка применимого текста согласия и правил его документирования",
    "advertising_consent": (
        "Подготовка отдельного рекламного согласия, если его необходимость установлена"
    ),
    "medical_disclosure_documents": (
        "Подготовка применимых юридических текстов медицинских раскрытий"
    ),
    "legal_webmaster_specification": (
        "Подробная письменная методичка/ТЗ вебмастеру по согласованным документам"
    ),
}


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def unique(values):
    return sorted(set(values))


class Qualification:
    def __init__(self, report: PrimaryAudit):
        self.report = report
        self.materials = {m.evidence_id: m for m in report.materials}
        self.observations = {o.id: o for o in report.observations}
        self.declarations = {d.id: d for d in report.declarations}
        self.findings = []
        self.work = []
        self.linked_document_observations = set()

    def object(self, kind, source_id):
        return ReviewObject(type=kind, source_id=source_id)

    def traceable(self, citation):
        material = self.materials.get(citation.evidence_id)
        return bool(
            material
            and str(citation.url) == str(material.url)
            and citation.artifact_path
            and citation.artifact_sha256
            and any(
                a.path == citation.artifact_path
                and a.sha256 == citation.artifact_sha256
                and a.kind in {"html", "pdf"}
                for a in material.artifacts
            )
            and (
                (citation.static_span and citation.static_span.status != "unknown")
                or (
                    citation.static_span is None
                    and citation.basis == "reproduced_text"
                    and material.reading.provenance == "reproduced"
                )
            )
        )

    def fact(self, description, objects, citations, *, observations=(), declarations=()):
        by_value = {c.model_dump_json(): c for c in citations}
        citations = [by_value[k] for k in sorted(by_value)]
        value = {
            "description": description,
            "objects": [o.model_dump() for o in objects],
            "citations": [c.model_dump(mode="json") for c in citations],
        }
        return FindingFact(
            id="fact-" + digest(value),
            description=description,
            objects=objects,
            citations=citations,
            observation_ids=sorted(observations),
            declaration_ids=sorted(declarations),
            traceability="traceable_saved_material"
            if all(self.traceable(c) for c in citations)
            else "incomplete",
        )

    def observed(self, observation):
        objects = [
            self.object(
                "document"
                if c.evidence_id in self.materials
                and any(a.kind == "pdf" for a in self.materials[c.evidence_id].artifacts)
                else "page",
                c.evidence_id,
            )
            for c in observation.citations
        ]
        objects = list({(o.type, o.source_id): o for o in objects}.values())
        return self.fact(
            observation.description, objects, observation.citations, observations=[observation.id]
        )

    def coverage(self, facts):
        levels = []
        for fact in facts:
            for c in fact.citations:
                material = self.materials.get(c.evidence_id)
                if not material or not self.traceable(c):
                    levels.append("unknown")
                elif c.static_span:
                    levels.append(c.static_span.status)
                else:
                    levels.append(
                        "complete"
                        if material.reading.text_coverage_complete
                        else "partial"
                        if material.reading.extraction_state == "partial"
                        else "unknown"
                    )
        return (
            "unknown" if "unknown" in levels else "partial" if "partial" in levels else "complete"
        )

    def finding(self, category, objects, facts, *, scope_key=None):
        # Exact source content is part of the stable key: edited materials invalidate identity.
        identity = {
            "category": category,
            "objects": [o.model_dump() for o in objects],
            "source_hashes": unique(
                c.artifact_sha256 or "unknown" for f in facts for c in f.citations
            ),
        }
        if scope_key is not None:
            identity["content_scope"] = scope_key
        facts = sorted({f.id: f for f in facts}.values(), key=lambda f: f.id)
        finding = LegalFinding(
            id="finding-" + digest(identity),
            category=category,
            objects=objects,
            factual_description=TOPICS[category],
            facts=facts,
            established_fact_ids=[
                f.id for f in facts if f.traceability == "traceable_saved_material"
            ],
            evidence_completeness=self.coverage(facts),
            chain_status="traceable_saved_material"
            if all(f.traceability == "traceable_saved_material" for f in facts)
            else "incomplete",
            unconfirmed_assumptions=UNKNOWN[category],
            additional_checks=CHECKS[category],
            manual_review_priority="P1" if category in {"A", "B", "C", "D", "G"} else "P2",
            priority_rationale="Сначала установить применимость юридического документа"
            if category in {"A", "B", "C", "D", "G"}
            else "Сначала подтвердить фактическое поведение или независимые роли",
            limitations=unique(
                [
                    (
                        "Только сохранённые области; отсутствие на странице не "
                        "означает отсутствие на сайте"
                    ),
                    (
                        "Доказательная цепочка не является аутентификацией "
                        "источника или юридической экспертизой"
                    ),
                    *[
                        v
                        for f in facts
                        for c in f.citations
                        for v in (
                            c.static_span.limitations
                            if c.static_span
                            else self.materials[c.evidence_id].reading.limitations
                            if c.evidence_id in self.materials
                            else ["Unknown source material"]
                        )
                    ],
                ]
            ),
        )
        self.findings.append(finding)
        return finding

    def candidate(self, work_type, finding, facts):
        # Technical uncertainty/absence alone never crosses this commercial scope boundary.
        supported = [f.id for f in facts if f.traceability == "traceable_saved_material"]
        if (
            not supported
            or finding.chain_status != "traceable_saved_material"
            or finding.evidence_completeness == "unknown"
        ):
            return
        existing = next((w for w in self.work if w.work_type == work_type), None)
        if existing:
            existing.legal_finding_ids = unique([*existing.legal_finding_ids, finding.id])
            existing.support_fact_ids = unique([*existing.support_fact_ids, *supported])
            return
        self.work.append(
            CommercialWorkCandidate(
                id="work-" + digest({"type": work_type, "dossier": self.report.dossier_sha256}),
                work_type=work_type,
                legal_finding_ids=[finding.id],
                support_fact_ids=sorted(supported),
                discovered_scope=(
                    "Конкретный сохранённый текст/контекст позволяет "
                    "предварительно очертить предмет, не необходимость "
                    "работы"
                ),
                client_clarifications=[
                    "Подтвердить фактического исполнителя/оператора и цели обработки",
                    "Предоставить действующие документы, процессы и распределение ролей",
                    (
                        "Уточнить, требуется ли подготовка нового текста или "
                        "действующий документ достаточен"
                    ),
                ],
                expected_paid_result=WORK_RESULTS[work_type],
                necessity_conditions=[
                    "Независимая юридическая проверка актуальных норм и применимости",
                    (
                        "Заказчик подтвердил фактическую модель; установлен "
                        "конкретный недостаток или потребность"
                    ),
                    (
                        "Действующий комплект не покрывает подтверждённую "
                        "потребность; объём отдельно согласован"
                    ),
                ],
                limitations=[
                    "Условный кандидат, не коммерческое предложение и не доказательство нарушения",
                    "Частичные фрагменты не подтверждают качество всего документа",
                    (
                        "Технические проверки предшествуют квалификации; "
                        "исправление сайта не входит в основной продукт"
                    ),
                ],
            )
        )

    def organizations(self):
        facts = []
        objects = []
        for org in self.report.organizations:
            obj = self.object("organization", org.id)
            fact = self.fact(
                f"Отдельное текстовое упоминание: {org.name}; роль не подтверждена",
                [obj],
                org.identification_sources,
            )
            facts.append(fact)
            objects.append(obj)
            self.finding("A", [obj], [fact])
        if len(objects) > 1:
            self.finding("H", objects, facts)

    def document_facts(self, form, stage):
        # No implicit association of another entity's document from an unrelated page.
        allowed = set(form.policy_links if stage == "04" else form.consent_links)
        return [
            self.observed(o)
            for o in self.report.observations
            if o.stage_id == stage
            and o.kind != "scoped_search"
            and any(c.evidence_id == form.evidence_id or str(c.url) in allowed for c in o.citations)
        ]

    def forms(self):
        for form in self.report.forms:
            obj = self.object("form", form.id)
            declarations = [d for d in self.report.declarations if d.form_id == form.id]
            fact = self.fact(
                f"Сохранена форма: {len(form.fields)} наблюдаемых полей; "
                f"{sum(f.type == 'checkbox' for f in form.fields)} наблюдаемых checkbox; "
                "фактическая отправка не проверена",
                [obj],
                [form.citation, *[d.citation for d in declarations]],
                declarations=[d.id for d in declarations],
            )
            if not self.report.organizations:
                self.finding("A", [obj], [fact])
            policy_facts = self.document_facts(form, "04")
            self.linked_document_observations.update(
                i for f in policy_facts for i in f.observation_ids
            )
            finding = self.finding("B", [obj], [fact, *policy_facts])
            # A real text marker plus form is scope support; missing policy/checkbox is not.
            content = [
                f
                for f in policy_facts
                if any(
                    c.quote
                    and len(c.quote) >= 80
                    and re.search(r"персональн|обработк|personal data", c.quote, re.I)
                    for c in f.citations
                )
            ]
            if content:
                self.candidate("policy_document", finding, [fact, *content])
            consent_facts = self.document_facts(form, "06")
            self.linked_document_observations.update(
                i for f in consent_facts for i in f.observation_ids
            )
            finding = self.finding("C", [obj], [fact, *consent_facts])
            if re.search(r"соглас|consent", form.submission_context, re.I) and consent_facts:
                self.candidate("processing_consent", finding, [fact, *consent_facts])
            if form.advertising_links or re.search(
                r"реклам|рассыл|маркетинг|newsletter|promotional", form.submission_context, re.I
            ):
                finding = self.finding("D", [obj], [fact, *consent_facts])
                if re.search(r"соглас|consent", form.submission_context, re.I) and consent_facts:
                    self.candidate("advertising_consent", finding, [fact, *consent_facts])

    def standalone_documents(self):
        grouped = {}
        for observation in self.report.observations:
            if observation.stage_id not in {"04", "06"} or observation.kind == "scoped_search":
                continue
            if observation.id in self.linked_document_observations:
                continue
            fact = self.observed(observation)
            key = (
                "B" if observation.stage_id == "04" else "C",
                tuple((o.type, o.source_id) for o in fact.objects),
            )
            grouped.setdefault(key, []).append(fact)
        for (category, _), facts in sorted(grouped.items()):
            self.finding(category, facts[0].objects, facts)

    def integrations(self):
        for category, selected in (
            ("E", [g for g in self.report.integration_groups if g.kind == "booking"]),
            (
                "F",
                [
                    g
                    for g in self.report.integration_groups
                    if g.service_kind
                    in {"google_tag_manager", "google_analytics", "yandex_metrika"}
                ],
            ),
        ):
            facts = []
            objects = []
            for group in selected:
                obj = self.object("integration", group.id)
                objects.append(obj)
                rows = [self.declarations[i] for i in group.occurrence_ids]
                facts.append(
                    self.fact(
                        f"Группа объявлений {group.service_kind or 'external_booking_candidate'}: "
                        f"{len(rows)} вхождений; не формы/перенаправления/передача",
                        [obj],
                        [d.citation for d in rows],
                        declarations=[d.id for d in rows],
                    )
                )
            if facts:
                self.finding(category, objects, facts)

    def medical(self):
        if self.report.sector_hint != "medical":
            return
        rows = [
            o for o in self.report.observations if o.stage_id == "12" and o.kind == "text_quote"
        ]
        if not rows:
            return
        facts = [self.observed(o) for o in rows]
        objects = list({(o.type, o.source_id): o for f in facts for o in f.objects}.values())
        finding = self.finding("G", objects, facts)
        substantive = [
            c
            for c in self.report.medical_checks
            if c.content_level == "content_candidate"
            and c.check_id in {"services", "prices", "medical_staff", "paid_service_rules"}
        ]
        if len(substantive) >= 2:
            selected = {i for c in substantive for i in c.observation_ids}
            self.candidate(
                "medical_disclosure_documents",
                finding,
                [f for f in facts if set(f.observation_ids) & selected],
            )

    def content_questions(self):
        codes = {
            "mailto_recipient_mismatch": "A",
            "document_image_unread": "G",
            "price_discrepancy_candidate": "G",
            "policy_response_period_candidate": "B",
            "policy_contact_url_candidate": "B",
            "cross_border_policy_review": "B",
            "archived_program_reference": "G",
            "dated_program_role_review": "G",
        }
        for observation in self.report.observations:
            code = observation.description.split(":", 1)[0]
            if code not in codes:
                continue
            fact = self.observed(observation)
            finding = self.finding(codes[code], fact.objects, [fact], scope_key=(code, fact.id))
            finding.factual_description = observation.description
            finding.limitations = unique([*finding.limitations, observation.scope])

    def result(self):
        self.organizations()
        self.forms()
        self.standalone_documents()
        self.integrations()
        self.medical()
        self.content_questions()
        if self.work:
            # Written legal implementation specification, never a technical repair offer.
            related = unique(i for w in self.work for i in w.legal_finding_ids)
            facts = unique(i for w in self.work for i in w.support_fact_ids)
            self.work.append(
                CommercialWorkCandidate(
                    id="work-"
                    + digest(
                        {
                            "type": "legal_webmaster_specification",
                            "dossier": self.report.dossier_sha256,
                        }
                    ),
                    work_type="legal_webmaster_specification",
                    legal_finding_ids=related,
                    support_fact_ids=facts,
                    discovered_scope=(
                        "Есть условные документные кандидаты; техническое внедрение не предлагается"
                    ),
                    client_clarifications=[
                        "Согласовать подтверждённые документы и возможности вебмастера"
                    ],
                    expected_paid_result=WORK_RESULTS["legal_webmaster_specification"],
                    necessity_conditions=[
                        "Документные работы действительно необходимы и отдельно согласованы",
                        "Письменные требования нужны для внедрения утверждённых документов",
                    ],
                    limitations=[
                        "Не включает исправление сайта или неопределённую техническую диагностику",
                        "Подробное ТЗ отсутствует в бесплатном предварительном обзоре",
                    ],
                )
            )
        return InternalAssessment(
            source_primary_sha256=digest(self.report.model_dump(mode="json")),
            source_dossier_sha256=self.report.dossier_sha256,
            source_origin=self.report.source_origin,
            materials=self.report.materials,
            legal_findings=sorted(self.findings, key=lambda f: (f.category, f.id)),
            commercial_work=sorted(self.work, key=lambda w: w.work_type),
            assessed_stage_ids=[s.stage_id for s in self.report.stages],
            additional_checks=unique(
                [v for f in self.findings for v in f.additional_checks]
                + [task.question for task in self.report.missing_checks]
            ),
            limitations=unique(
                [
                    *self.report.limitations,
                    "Квалификация — детерминированные вопросы, не юридическая экспертиза",
                    (
                        "Непроверенные редакции НПА не используются для "
                        "последствий, штрафов или обязанностей"
                    ),
                    (
                        "Прежний commercial-opportunity.json — legacy scope "
                        "hints, не квалифицированное предложение"
                    ),
                    "Полное техническое досье и ТЗ не входят в предварительный внутренний обзор",
                ]
            ),
        )


def qualify_primary(report: PrimaryAudit) -> InternalAssessment:
    """Pure derivation; imported claims never become trusted legal conclusions."""
    return Qualification(report).result()


def preliminary_outline(assessment: InternalAssessment) -> PreliminaryClientSummary:
    # Controlled high-level templates, no interpolation of HTML/quotes/repair instructions.
    return PreliminaryClientSummary(
        source_assessment_sha256=digest(assessment.model_dump(mode="json")),
        directions=[
            SummaryDirection(category=c, attention_reason=SUMMARY_REASONS[c])
            for c in unique(f.category for f in assessment.legal_findings)
        ],
    )
