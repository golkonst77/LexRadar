"""Escaped standalone HTML and hash-bound, unauthenticated expert annotations."""

import html
import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from .models import ExpertLabel, TestReport


def load_report(root):
    path = root / "test-report.json"
    if path.is_symlink() or path.stat().st_size > 6_000_000:
        raise ValueError("Unsafe quality report")
    content = path.read_bytes()
    return TestReport.model_validate_json(content), sha256(content).hexdigest()


def labels_for(root, path):
    report, sha = load_report(root)
    if path.is_symlink() or path.stat().st_size > 1_000_000:
        raise ValueError("Unsafe expert annotation file")
    labels = [ExpertLabel.model_validate(v) for v in json.loads(path.read_text())]
    cases = {c.id: c for c in report.cases}
    seen = set()
    for label in labels:
        case = cases.get(label.case_id)
        if (
            label.report_sha256 != sha
            or case is None
            or label.dossier_sha256 != case.dossier_sha256
            or label.finding_id not in {p.id for p in case.predictions}
            or not label.reviewer.strip()
            or label.reviewed_at > datetime.now(UTC)
            or label.reviewed_at < report.created_at
            or (label.case_id, label.finding_id) in seen
        ):
            raise ValueError("Stale, duplicate or unrelated expert annotation")
        seen.add((label.case_id, label.finding_id))
    return labels


def _e(value):
    return html.escape(str(value), quote=True)


def evidence_link(path):
    p = PurePosixPath(path)
    if not path.startswith("cases/") or p.is_absolute() or ".." in p.parts or "\\" in path:
        raise ValueError("Unsafe evidence link")
    return quote(path, safe="/-._")


def write_html(root: Path, expert_path=None):
    report, report_hash = load_report(root)
    labels = labels_for(root, expert_path) if expert_path else []
    lines = [
        '<!doctype html><html lang="ru"><meta charset="utf-8">',
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        "style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">",
        "<title>LexRadar — испытания</title><style>body{font:16px system-ui;max-width:1100px;"
        "margin:2em auto;padding:1em}table{border-collapse:collapse;width:100%}"
        "td,th{border:1px solid #ccc;padding:.4em;text-align:left}"
        "code{overflow-wrap:anywhere}</style>",
        "<h1>LexRadar: испытательный отчёт</h1>",
        "<p>ТОЛЬКО СИНТЕТИЧЕСКИЕ/REPLAY ИСПЫТАНИЯ. Положительные эталонные метки не являются "
        "юридическим GO или допуском клиентского текста.</p>",
        "<p>Эталонные сценарии и записанные ответы. Результаты не оценивают качество реальных LLM "
        "и не устанавливают юридическое соответствие сайта.</p>",
        "<p>Статус: "
        + _e(report.overall_status)
        + "</p><p>SHA-256 отчёта: <code>"
        + _e(report_hash)
        + "</code></p>",
        "<h2>Метрики</h2><table><tr><th>Метрика</th>"
        "<th>Числитель / знаменатель</th><th>Значение</th></tr>",
    ]
    for name, metric in report.metrics.items():
        value = f"{metric.value:.4f}" if metric.value is not None else "not_applicable"
        lines.append(
            f"<tr><td>{_e(name)}</td><td>{metric.numerator}/{metric.denominator}</td>"
            f"<td>{value}</td></tr>"
        )
    lines.append("</table><h2>Слои оценки</h2>")
    for name, explanation in report.layers.items():
        lines.append(f"<p><b>{_e(name)}</b>: {_e(explanation)}</p>")
    for case in report.cases:
        lines.extend(
            [
                f"<h2>{_e(case.id)} ({_e(case.mode)})</h2>",
                f"<p>{_e(case.situation)}</p>",
                f"<p>Production decision: {_e(case.production_outcome or 'not evaluated')}; "
                "клиентский допуск отсутствует.</p>",
                f"<p>Досье: <code>{_e(case.dossier_sha256)}</code></p>",
                "<table><tr><th>Проверка</th><th>Результат</th><th>Основание</th></tr>",
            ]
        )
        for check in case.checks:
            lines.append(
                f"<tr><td>{_e(check.name)}</td><td>{'PASS' if check.passed else 'FAIL'}</td>"
                f"<td>{_e(check.detail)}</td></tr>"
            )
        lines.append("</table><table><tr><th>Эталон / находка</th><th>Результат</th></tr>")
        for label, values in [
            ("Обнаружено", case.found),
            ("Пропущено", case.missed),
            ("Ложноположительно / причина", case.false_positives),
            ("Ручная проверка", case.manual_review),
        ]:
            for value in values:
                lines.append(f"<tr><td>{_e(value)}</td><td>{_e(label)}</td></tr>")
        lines.append(
            "</table><p>Модели: "
            + _e(", ".join(case.models))
            + "</p><p>Промпты: "
            + _e(json.dumps(case.prompts))
            + "</p><p>API текущего запуска, USD: "
            + _e(case.api_cost_usd if case.api_cost_usd is not None else "unknown")
            + "</p>"
        )
        lines.append(
            "<p>Метаданные исходной записи (самозаявленные): "
            + _e(case.recording_metadata.model_dump_json())
            + "</p><table><tr><th>Находка</th><th>Статус / факт / норма / применимость</th>"
            "<th>Основания и ограничения</th></tr>"
        )
        for p in case.predictions:
            state = (
                f"{p.status}; grounded={p.fact_grounded}; "
                f"norm_verified={p.norm_verified}; {p.applicability}"
            )
            lines.append(
                f"<tr><td>{_e(p.id)}: {_e(p.claim_code)}</td><td>{_e(state)}</td>"
                f"<td>{_e('; '.join(p.reasons))}</td></tr>"
            )
        lines.append("</table>")
        lines.append(
            "<p>Артефакты — недоверенные оригиналы; HTML предлагается скачать. "
            "Скриншоты изолированы: JavaScript и внешние ресурсы отключены; "
            "это не точное воспроизведение живого сайта.</p><ul>"
        )
        for eid, paths in case.evidence_links.items():
            for path in paths:
                lines.append(
                    f'<li>{_e(eid)}: <a download href="{evidence_link(path)}">{_e(path)}</a></li>'
                )
        lines.append(
            "</ul><ul>" + "".join("<li>" + _e(v) + "</li>" for v in case.limitations) + "</ul>"
        )
    if labels:
        lines.append("<h2>Самозаявленная экспертная разметка — без удостоверения личности</h2>")
        for label in labels:
            lines.append(
                f"<p>{_e(label.case_id)} / {_e(label.finding_id)}: {_e(label.status)}; "
                f"{_e(label.rationale)}</p>"
            )
    lines.append(
        "<h2>Ограничения</h2><ul>"
        + "".join("<li>" + _e(v) + "</li>" for v in report.limitations)
        + "</ul></html>"
    )
    path = root / "test-report.html"
    if path.is_symlink():
        raise ValueError("Unsafe HTML output")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
