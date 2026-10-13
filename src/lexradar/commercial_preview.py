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
            signals_html = (
                "<li>Подтверждённые нарушения по собранным материалам "
                "не установлены.</li>"
            )
        cards.append(f"""<article>
<h2>{esc(hostname)}</h2><p><a href="{esc(url)}">{esc(url)}</a></p>
<p><strong>Статус предварительной проверки:</strong> {esc(row.get("technical_status", "не определён"))}
.
Проверена только ограниченная выборка общедоступных материалов.</p>
<h3>На что обратить внимание</h3><ul>{signals_html}</ul>
<details><summary>Технические замечания сборщика</summary><ul>{reasons_html}</ul></details>
<p>Для окончательного вывода необходимо проверить фактические формы, документы,
оператора персональных данных и применимые требования законодательства.</p>
</article>""")
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
{''.join(cards)}
<section><h2>Что можно сделать по результатам проверки</h2>
<p>Подготовить актуальные юридические документы для сайта и подробное техническое задание
вебмастеру для их самостоятельного внедрения. Объём и стоимость работ определяются
после уточнения обнаруженных вопросов.</p></section>
<footer>LexRadar · Предварительный информационный обзор</footer>
</main></body></html>"""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(document, encoding="utf-8")
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch_directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    count = render(args.batch_directory, args.output)
    print(f"Created human-review preview for {count} sites: {args.output}")


if __name__ == "__main__":
    main()
