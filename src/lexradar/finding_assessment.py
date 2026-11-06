"""Independent factual, legal and commercial decisions, stored outside AI schemas."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import AwareDatetime, Field

from .auditors.grounding import check_quote
from .auditors.models import TextGrounding
from .batch_qualification import analysis_digest
from .models import Model
from .policy3 import Policy3Review, inspect_policy3


class FindingReview(Model):
    finding_id: str
    quote_indices: list[int] = Field(default_factory=list, max_length=100)
    factual_scope: str = Field(min_length=20, max_length=5000)
    risk_reason: str = Field(min_length=20, max_length=5000)
    deliverables: list[str] = Field(min_length=1, max_length=10)
    services: list[Literal["legal_documents", "webmaster_instructions"]] = Field(
        min_length=1, max_length=2
    )
    positive_only: bool = Field(default=False, strict=True)
    factual_scope_verified: bool = Field(strict=True)
    service_need_verified: bool = Field(strict=True)
    legal_violation_verified: bool = Field(default=False, strict=True)
    verified_legal_sources: list[str] = Field(default_factory=list, max_length=20)
    contradiction: TextGrounding | None = None


class CommercialReview(Model):
    """Explicit operator judgment, never accepted from the model response."""

    packet_sha256: str
    analysis_sha256: str
    reviewer: str = Field(min_length=1, max_length=200)
    reviewed_at: AwareDatetime
    findings: list[FindingReview] = Field(max_length=100)
    policy3: list[Policy3Review] = Field(default_factory=list, max_length=3)


def offer_for(finding, indices):
    services = {
        "legal_documents": (
            "Проверка и подготовка юридических документов по указанному наблюдению",
            "Письменное заключение по наблюдению и проекты необходимых изменений документов "
            "после уточнения фактических процессов",
        ),
        "webmaster_instructions": (
            "Проверка реализации описанного механизма и подготовка задания вебмастеру",
            "Протокол проверки механизма и ТЗ вебмастеру с согласованными изменениями "
            "и критериями приёмки, если изменения необходимы",
        ),
    }
    scope = "\n".join(finding.text_grounding[i].exact_quote for i in indices)
    title = "Проверка опубликованных сведений"
    meaning = "; ".join(services[s][1] for s in finding.proposed_work)
    if (
        finding.topic.value == "cookies_analytics"
        and "метрик" in scope.casefold()
        and "продолжите использовать сайт" in scope.casefold()
    ):
        title = "Cookie и Яндекс.Метрика"
        results = []
        if "webmaster_instructions" in finding.proposed_work:
            results.append("протокол проверки и ТЗ вебмастеру")
        if "legal_documents" in finding.proposed_work:
            results.append("необходимые уточнения политики")
        meaning = (
            "Из текста уведомления неясно, можно ли отказаться от аналитики до её запуска. "
            "Проверим загрузку Метрики и cookies до выбора и после отказа; подготовим "
            + "; ".join(results)
            + "."
        )
    elif finding.topic.value == "special_categories":
        title = "Дополнительный вопрос по медицинским данным"
        meaning = (
            "Соответствуют ли положения политики о специальных категориях данных "
            "реальным процессам клиники и используемым согласиям?"
        )
        if "биометр" in scope.casefold() and "здоров" in scope.casefold():
            meaning = (
                "Соответствуют ли упомянутые в политике данные о здоровье и биометрия "
                "реальным процессам клиники и используемым согласиям?"
            )
    return dict(
        quote_indices=indices,
        factual_scope=scope,
        client_title=title,
        client_meaning=meaning,
        risk_reason="Возможное расхождение опубликованного описания с фактическими процессами "
        "или применимыми требованиями требует проверки. Нарушение не установлено.",
        services=finding.proposed_work,
        service_description=[services[s][0] for s in finding.proposed_work],
        deliverables=[services[s][1] for s in finding.proposed_work],
        # shortcut: AI checks are suggestions for internal review, never client-facing facts.
        suggested_checks=finding.additional_checks,
    )


def evaluate(result, packet, report, review=None, *, commercial_mvp=False):
    rows = []
    reviews = {}
    ids = {f.id for f in result.findings} if result else set()
    if review is not None:
        if (
            review.packet_sha256 != packet.sha256
            or review.analysis_sha256 != analysis_digest(report)
            or not review.reviewer.strip()
            or review.reviewed_at > datetime.now(UTC)
            or len({r.finding_id for r in review.findings}) != len(review.findings)
            or any(r.finding_id not in ids for r in review.findings)
        ):
            raise ValueError("Commercial review binding/identity mismatch")
        reviews = {r.finding_id: r for r in review.findings}
    for finding in result.findings if result else []:
        checks = [check_quote(packet, q) for q in finding.text_grounding]
        valid = [i for i, c in enumerate(checks) if c["status"] == "MATCH"]
        full_fact = finding.fact_assertion == "present" and any(
            finding.fact == finding.text_grounding[i].exact_quote for i in valid
        )
        factual = "CONFIRMED" if full_fact else "PARTIAL" if valid else "UNSUPPORTED"
        legal = "UNVERIFIED"
        classification = "unsubstantiated_assumption"
        if valid and (
            finding.legal_position == "no_issue"
            or (finding.status == "confirmed" and finding.legal_position == "undetermined")
        ):
            classification = "positive_fact"
        elif full_fact and (
            finding.legal_position == "possible_issue"
            or (
                finding.topic.value == "cookies_analytics"
                and result.cookie_analytics_review.status == "ISSUE"
            )
        ):
            classification = "confirmed_fact_with_legal_risk"
        elif valid:
            classification = "justified_hypothesis"
        item = reviews.get(finding.id)
        offer = None
        eligible = False
        reasons = ["operator_commercial_review_required"]
        if not valid:
            reasons = ["no_exact_source_bound_quote"]
        if finding.legal_position == "no_issue":
            reasons = ["positive_fact_is_not_a_service_need"]
        if item:
            indices = item.quote_indices
            if (
                len(indices) != len(set(indices))
                or not indices
                or any(i not in valid for i in indices)
            ):
                raise ValueError("Review must select independently matching quotations")
            if item.contradiction is not None:
                if (
                    not item.factual_scope_verified
                    or check_quote(packet, item.contradiction)["status"] != "MATCH"
                    or (
                        finding.fact_assertion == "present"
                        and item.contradiction.exact_quote == finding.fact
                    )
                ):
                    raise ValueError("Counter-evidence must be an exact source-bound quotation")
                factual = "CONTRADICTED"
                classification = "unsubstantiated_assumption"
                reasons = ["operator_verified_counter_evidence"]
            elif item.positive_only or finding.legal_position == "no_issue":
                classification = "positive_fact"
                reasons = ["positive_fact_is_not_a_service_need"]
            else:
                eligible = (
                    item.factual_scope_verified
                    and item.service_need_verified
                    and len(item.factual_scope.strip()) >= 20
                    and len(item.risk_reason.strip()) >= 20
                    and all(len(work.strip()) >= 10 for work in item.deliverables)
                    and finding.status != "rejected"
                    and report.completed
                    and report.runs[0].status == "success"
                )
                reasons = [] if eligible else ["commercial_argument_not_verified"]
                if eligible:
                    classification = "confirmed_fact_with_legal_risk"
                if item.legal_violation_verified:
                    if not (
                        eligible
                        and item.verified_legal_sources
                        and all(s.strip() for s in item.verified_legal_sources)
                    ):
                        raise ValueError("Verified violation requires independent legal proof")
                    classification = "confirmed_violation"
                    legal = "INDEPENDENTLY_REVIEWED"
        elif commercial_mvp:
            eligible = (
                full_fact
                and finding.material
                and finding.status in {"potential", "confirmed"}
                and classification != "positive_fact"
                and bool(finding.proposed_work)
                and bool(finding.additional_checks)
                and all(len(work.strip()) >= 10 for work in finding.additional_checks)
                and report.completed
                and report.runs[0].status == "success"
            )
            reasons = [] if eligible else ["commercial_mvp_criteria_not_met"]
        if (
            commercial_mvp
            and valid
            and finding.fact_assertion == "present"
            and finding.status != "rejected"
            and classification != "positive_fact"
            and factual != "CONTRADICTED"
            and finding.proposed_work
            and (item is None or eligible)
        ):
            indices = (
                [i for i in valid if finding.text_grounding[i].exact_quote == finding.fact]
                if eligible and full_fact
                else valid
            )
            offer = offer_for(finding, indices)
        rows.append(
            dict(
                id=finding.id,
                fact=finding.fact,
                fact_status=factual,
                fact_proven_scope="text_excerpt_only",
                actual_site_behavior_verified=False,
                classification=classification,
                legal_status=legal,
                legal_violation_proven=legal == "INDEPENDENTLY_REVIEWED",
                quotes=checks,
                confirmed_quote_indices=valid,
                confirmed_observations=[finding.text_grounding[i].exact_quote for i in valid],
                original_source=str(finding.source),
                source_urls=sorted(
                    {
                        m["source"]
                        for i in valid
                        for m in checks[i]["matches"]
                        if m["evidence_id"] == finding.text_grounding[i].evidence_id
                    }
                ),
                commercial_candidate=bool(valid)
                and classification != "positive_fact"
                and factual != "CONTRADICTED",
                commercial_eligible=eligible,
                commercial_reasons=reasons,
                operator_review=item.model_dump(mode="json") if item else None,
                commercial_offer=offer,
                proposal_role=None,
                limitation="Exact occurrence proves text, not every factual or legal inference.",
            )
        )
    allowed = [r["id"] for r in rows if r["commercial_eligible"]]
    proposal_findings = allowed[:4]
    supporting = []
    if commercial_mvp and allowed:
        topics = {f.id: f.topic.value for f in result.findings}
        primary = next(
            (
                r
                for r in rows
                if r["commercial_eligible"] and topics[r["id"]] == "cookies_analytics"
            ),
            next(r for r in rows if r["commercial_eligible"]),
        )
        primary["proposal_role"] = "primary"
        supporting = [
            r
            for r in rows
            if r is not primary
            and r["commercial_offer"] is not None
            and topics[r["id"]] == "special_categories"
        ][:1]
        for row in supporting:
            row["proposal_role"] = "hypothesis"
        proposal_findings = [primary["id"], *(r["id"] for r in supporting)]
    reasons = (
        []
        if allowed
        else [
            "no_eligible_commercial_mvp_finding"
            if commercial_mvp
            else "no_documented_service_need_approved"
        ]
    )
    if not report.completed or report.runs[0].status != "success":
        reasons.insert(0, "auditor_failed")
    return dict(
        schema_version="102",
        policy3=inspect_policy3(packet, review),
        commercial_mode="mvp" if commercial_mvp else "review",
        packet_sha256=packet.sha256,
        analysis_sha256=analysis_digest(report),
        findings=rows,
        outcome="GO" if allowed else "HOLD",
        allowed_findings=allowed,
        proposal_findings=proposal_findings,
        supporting_findings=[r["id"] for r in supporting],
        reasons=reasons,
        automatic_send_allowed=False,
        client_release_allowed=False,
        manual_approval_required=True,
        manual_approval=None,
        sent=False,
    )


def review_template(report, packet):
    return dict(
        packet_sha256=packet.sha256,
        analysis_sha256=analysis_digest(report),
        reviewer="",
        reviewed_at=None,
        findings=[],
        policy3=[],
    )
