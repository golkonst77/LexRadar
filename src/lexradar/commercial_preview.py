"""Generate a clearly qualified, human-reviewable commercial preview from saved batch results.

This does not alter the production decision or authorize automatic client release.
"""

import argparse
import html
import json
from pathlib import Path
from urllib.parse import urlsplit


def esc(value):
    return html.escape(str(value or ""), quote=True)


def render(root: Path, output: Path) -> int:
    queue = json.loads((root / "review-queue.json").read_text(encoding="utf-8"))
    rows = queue.get("rows", [])
    cards = []
    for row in rows:
        url = row.get("url", "")
        hostname = urlsplit(url).netloc
        reasons = row.get("reasons") or []
        findings = row.get("signals") or []
        reasons_html = "".join("<li>" + esc(x) + "</li>" for x in reasons)
        signals_html = ""
        for signal in findings:
            if not isinstance(signal, dict):
                continue
            reason = signal.get("reason", "Потенциальный вопрос для проверки")
            signals_html += "<li>" + esc(reason) + "</li>"
        if not signals_html:
            signals_html = "<li>Подтверждённые нарушения не установлены.</li>"
        status = esc(row.get("technical_status", "не определён"))
        cards.append(
            f"""<article>
<h2>{esc(hostname)}</h2><p><a href="{esc(url)}">{esc(url)}</a></p>
<p><strong>Статус предварительной проверки:</strong> {status}.
Проверена только ограниченная выборка общедоступных материалов.</p>
<h3>На что обратить внимание</h3><ul>{signals_html}</ul>
<details><summary>Технические замечания сборщика</summary><ul>{reasons_html}</ul></details>
<p>Для окончательного вывода необходимо проверить фактические формы, документы,
оператора персональных данных и применимые требования законодательства.</p>
</article>"""
        )
    cards_html = "".join(cards)
    write_document(cards_html, output)
    return len(rows)


def write_document(cards_html, output):
    document = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LexRadar — предварительный обзор сайтов</title>
<style>
body{{font:16px/1.6 system-ui,Arial,sans-serif;background:#f5f7fb;color:#182338;margin:0}}
main{{max-width:850px;margin:40px auto;padding:0 20px}}
h1{{font-size:34px;line-height:1.2}}h2{{margin-bottom:4px}}
article{{background:white;padding:28px;margin:22px 0;border:1px solid #dbe2ed;border-radius:16px}}
.notice{{border-left:4px solid #bd7131;padding:12px 18px;background:#fff8ec}}
a{{color:#1756aa;overflow-wrap:anywhere}}
footer{{padding:28px 0;color:#566579}}
@media print{{body{{background:white}}article{{break-inside:avoid}}}}
</style></head><body><main>
<h1>Предварительная проверка сайтов</h1>
<p class="notice"><strong>Черновик для проверки специалистом.</strong>
Автоматический обзор не подтверждает наличие правонарушений и не является юридическим заключением.
Распространение клиентам допускается только после проверки фактов ответственным специалистом.</p>
{cards_html}
<section><h2>Что можно сделать по результатам проверки</h2>
<p>ИП Голубев предлагает платную подготовку юридических документов для сайта и подробной инструкции
вебмастеру для самостоятельного внедрения. Внедрение выполняет ваш вебмастер.
Объём и стоимость работ определяются
после уточнения обнаруженных вопросов.</p></section>
<footer>LexRadar · Предварительный информационный обзор</footer>
</main></body></html>"""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(document, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch_directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    count = render(args.batch_directory, args.output)
    print(f"Created human-review preview for {count} sites: {args.output}")


if __name__ == "__main__":
    main()


def safe_link(url, label=None):
    try:
        parsed = urlsplit(str(url))
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username:
            return esc(label or url)
    except ValueError:
        return esc(label or url)
    return f'<a href="{esc(url)}">{esc(label or url)}</a>'


def render_mvp(root: Path, output: Path, *, fixture=False):
    """Use local primary evidence, never turn fake AI hypotheses into violations."""
    collection = json.loads((root / "dossier/collection.json").read_text())
    primary = json.loads((root / "analysis/primary.local.json").read_text())
    analysis = json.loads((root / "analysis/compact-analysis.json").read_text())
    traces = json.loads((root / "analysis/trace-map.local.json").read_text())
    facts = []
    for page in collection["pages"]:
        if not page.get("text"):
            continue
        facts.append(
            f"<li>{safe_link(page.get('final_url') or page['requested_url'])}: "
            f"HTTP {esc(page.get('http_status'))}; "
            f"фрагмент сохранённого текста: «{esc(page['text'][:400])}».</li>"
        )
    for doc in collection["documents"]:
        facts.append(
            f"<li>{safe_link(doc.get('final_url') or doc['requested_url'])}: "
            f"PDF, извлечение текста: {esc(doc['extraction_status'])}; "
            f"«{esc((doc.get('text') or '')[:400])}».</li>"
        )
    for form in primary["forms"]:
        fields = ", ".join(
            field.get("label") or field.get("name") or field["type"] for field in form["fields"]
        )
        checkboxes = sum(field["type"] == "checkbox" for field in form["fields"])
        facts.append(
            f"<li>{safe_link(form['url'])}: форма «{esc(form['purpose_hint'])}», "
            f"поля: {esc(fields)}. В проверенном статическом фрагменте "
            f"checkbox: {checkboxes}; ссылок на политику внутри формы: "
            f"{len(form['policy_links'])}. Передача формы не тестировалась.</li>"
        )
    observations = []
    for item in (analysis.get("result") or {}).get("observations", []):
        trace = traces[item["excerpt"]]
        observations.append(
            f"<li>{safe_link(trace['url'])}: «{esc(item['quote'])}». "
            "Цитата проверена по локальному оригиналу. Синтетический AI "
            "пометил её для ручной проверки; это не правовой вывод.</li>"
        )
    limitations = esc(
        json.dumps(primary["limitations"] + collection["warnings"], ensure_ascii=False)
    )
    badge = (
        "СИНТЕТИЧЕСКОЕ ДЕМО: страницы и PDF созданы для проверки сценария."
        if fixture
        else "Собраны общедоступные материалы сайта; AI-ответ синтетический."
    )
    cards = f"""<article><h2>Предварительное коммерческое предложение</h2>
<p class="notice">{badge} Платных AI-запросов не было.</p>
<p>Сайт: {safe_link(collection["target_url"])}. Дата сбора:
{esc(collection["started_at"])}.</p>
<h3>Подтверждённые технические факты</h3><ul>{"".join(facts)}</ul>
<h3>Потенциальные риски и вопросы для проверки</h3>
<p>Если формы собирают персональные данные, необходимо уточнить оператора, цели,
сроки обработки, содержание политики и механизм согласия. Наличие ссылки само по себе
не подтверждает достаточность документа. Отсутствие элемента в выборке не доказывает
его отсутствие на всём сайте. Нарушения и применимость правовых требований не установлены.</p>
<ul>{"".join(observations)}</ul>
<h3>Предлагаемый результат платной работы</h3>
<p>ИП Голубев подготовит согласованный комплект юридических документов с учётом
реальных процессов: политику обработки персональных данных, необходимые согласия,
а при применимости — условия заказа. Вы получите подробную инструкцию вебмастеру:
где разместить документы, как связать их с формами и какие проверки выполнить после внедрения.</p>
<p>Первый шаг — уточнить процессы и проверить материалы со специалистом.
После этого согласуем состав документов, срок и стоимость. Оплата — за подготовку
документов и инструкции; внедрение выполняет ваш вебмастер.</p>
<details><summary>Границы проверки и артефакты</summary>
<p>Ограниченная статическая выборка; динамические формы и визуальный интерфейс требуют
отдельной проверки. {limitations}</p>
<p><a href="dossier/collection.json">Досье</a> ·
<a href="analysis/primary.local.json">Факты первичного анализа</a> ·
<a href="analysis/compact-analysis.json">Синтетический AI-анализ</a> ·
<a href="analysis/trace-map.local.json">Карта цитат</a> ·
<a href="sha256.json">Контрольные суммы</a></p>
<p>HOLD; юридические выводы не подтверждены; автоматическая отправка отключена.</p>
</details></article>"""
    write_document(cards, output)
