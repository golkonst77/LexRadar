"""Build the static review of the saved synthetic pilot; no network or approval."""

import hashlib
import json
from datetime import datetime
from html import escape
from pathlib import Path

from lexradar.auditors.budget import Budget
from lexradar.auditors.compact_pipeline import messages_for_compact
from lexradar.auditors.models import AnalysisConfig
from lexradar.auditors.preflight import PATTERNS, screen
from lexradar.primary.compact import prepare, unpack

BASE = Path(__file__).resolve().parent


def details(title, content):
    return f"<details><summary>{escape(title)}</summary><pre>{escape(content)}</pre></details>"


def main():
    packet = prepare(BASE / "dossier", as_of=datetime.fromisoformat("2026-10-10T00:00:00+00:00"))
    saved = (BASE / "review/packet.json").read_bytes()
    if saved != packet.payload.encode():
        raise ValueError("Saved packet differs; review must be regenerated explicitly")
    config = AnalysisConfig.model_validate_json(
        (BASE.parent / "compact-openrouter-config.json").read_text()
    )
    messages = messages_for_compact(packet)
    reserve = Budget(config.max_budget_usd).reserve(config.auditor_a, messages)
    input_bound = sum(len(m["content"].encode()) for m in messages) + 1024
    rows = []
    for key, trace in json.loads(packet.trace_json).items():
        artifact = trace["artifact_path"]
        source = "Нет исходного артефакта; только manifest"
        if artifact:
            path = BASE / "dossier" / artifact
            if hashlib.sha256(path.read_bytes()).hexdigest() != trace["artifact_sha256"]:
                raise ValueError("Artifact hash mismatch")
            source = f'<a href="../dossier/{escape(artifact, quote=True)}">{escape(artifact)}</a>'
        quote = trace["quote"] or "Цитата отсутствует; см. element/static_span"
        rows.append(
            f"<tr><td>{escape(key)}</td><td>{escape(quote)}</td><td>{source}"
            f"<p>{escape(trace['url'])}</p><p>page: {escape(str(trace['page']))}; "
            f"basis: {escape(trace['basis'])}</p>"
            f"<code>{escape(trace['artifact_sha256'] or 'нет SHA')}</code>"
            f"{details('Полная трассировка', json.dumps(trace, ensure_ascii=False, indent=2))}"
            "</td></tr>"
        )
    categories = "".join(
        f"<li>{escape(category)}: {sum(i['category'] == category for i in screen(packet))}</li>"
        for category in PATTERNS
    )
    html = f"""<!doctype html>
<html lang="ru"><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy"
 content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LexRadar — просмотр синтетического пакета №32</title>
<style>
body{{font:16px system-ui;margin:2rem auto;padding:0 1rem;max-width:1200px;color:#172b3a}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f2f5f7;padding:1rem}}
code{{overflow-wrap:anywhere}} table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #ccd5db;padding:.7rem;text-align:left;vertical-align:top}}
summary{{cursor:pointer;padding:.6rem;background:#eaf0f4}} p{{line-height:1.5}}
</style>
<h1>Пакет №32: личный просмотр</h1>
<p>Синтетический сайт clinic.example, пустая форма и учебные PDF.
BLOCKED: человеческое подтверждение отсутствует.
siliconflow/fp8 заявляет status=0; реальный запрос не проверен.
HOLD / not_confirmed / client_release_allowed=false.</p>
<p>Этот файл статический: без JavaScript, сервера, внешних ресурсов и отправки данных.
Ссылки на доказательства открывают файлы из соседнего dossier; адреса сайта показаны текстом.</p>
<h2>SHA-256 точных байтов user payload</h2><code>{packet.sha256}</code>
<p>{len(saved)} байт. <a href="packet.json">Исходный packet.json</a>.
SHA относится к packet.json, а не к HTML или полному HTTP-конверту.</p>
<h2>Что отправляется в OpenRouter</h2>
<p>System: инструкция Auditor A и JSON-схема ответа. User: весь packet.json,
включая primary, excerpts, dictionary, keys, ограничения и статусы.
Ниже развёрнутый body для удобства чтения; исходящий user payload остаётся сжатым.
Исходные HTML/PDF/PNG, локальные primary и trace-map не отправляются.</p>
<p>Модель deepseek/deepseek-v3.2; only=[siliconflow/fp8]; max_tokens=2000;
reasoning.enabled=false; response_format=json_object; temperature=0; stream=false; n=1;
allow_fallbacks=false; require_parameters=true; retries=0; max_price=0.259/0.80.</p>
{details("Точный system prompt", messages[0]["content"])}
{details("Точный user payload", packet.payload)}
{details("Развёрнутый body", json.dumps(unpack(packet), ensure_ascii=False, indent=2))}
<h2>Screening</h2><ul>{categories}</ul>
<p>Совпадений нет. Проверка эвристическая и не гарантирует отсутствие чувствительных
данных; требуется личный просмотр. Категории и паттерны не отключены.</p>
<h2>Расчётная стоимость</h2>
<p>Вход $0.259 / миллион, выход $0.80 / миллион.
Резерв: ({input_bound} × 0.259 + 2000 × 0.80) / 1000000 = ${reserve}.
Вход — консервативный предел UTF-8 байтов system/user + 1024 overhead;
фактические токены неизвестны. Лимит $0.05. Расход $0; запрос не выполнен.</p>
<h2>Цитаты и локальные доказательства</h2>
<table><thead><tr><th>ID</th><th>Исходная цитата</th><th>Доказательство</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table>
<h2>Как подтвердить SHA</h2>
<p>Раскройте оба сообщения и body; проверьте цитаты и оригиналы. Затем из корня
репозитория выполните:</p>
<pre>sha256sum examples/compact-first-pilot/review/packet.json</pre>
<p>Если SHA совпадает и вы лично просмотрели содержимое, сообщите:
«Пакет просмотрен; подтверждаю SHA-256 {packet.sha256}».
Это сообщение фиксирует просмотр; разрешение платной отправки оформляется отдельно.
HTML не создаёт approval. <a href="../../../docs/compact-first-pilot.md">Полная инструкция</a>.</p>
</html>"""
    (BASE / "review/index.html").write_text(html, encoding="utf-8")
    print(f"Static review built: {packet.sha256}; reserve=${reserve}")


if __name__ == "__main__":
    main()
