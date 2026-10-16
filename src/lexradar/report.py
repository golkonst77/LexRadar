"""Legacy/demo dossier and synthetic draft; never a production client document."""

from .gateway import decide_demo
from .models import AuditInput, AuditReport, Outcome


def build_demo_report(data: AuditInput) -> AuditReport:
    decision = decide_demo(data)
    draft = None
    if decision.outcome == Outcome.GO:
        draft = (
            "DEMO/LEGACY — НЕ ДЛЯ ОТПРАВКИ КЛИЕНТУ. "
            "Черновик для проверки человеком. Предлагаем обсудить результаты "
            "юридико-технического аудита и возможные меры по устранению "
            "подтверждённых вопросов. Основания и доказательства приведены во внутреннем досье."
        )
    return AuditReport(dossier=data, decision=decision, client_message_draft=draft)


# Retained legacy API; its output is explicitly marked and cannot grant production GO.
build_report = build_demo_report
