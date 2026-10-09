"""Internal dossier and restrained draft; no transport exists in this MVP."""

from .gateway import decide
from .models import AuditInput, AuditReport, Outcome


def build_report(data: AuditInput) -> AuditReport:
    decision = decide(data)
    draft = None
    if decision.outcome == Outcome.GO:
        draft = (
            "Черновик для проверки человеком. Предлагаем обсудить результаты "
            "юридико-технического аудита и возможные меры по устранению "
            "подтверждённых вопросов. Основания и доказательства приведены во внутреннем досье."
        )
    return AuditReport(dossier=data, decision=decision, client_message_draft=draft)
