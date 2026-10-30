"""Invoke the existing real rules engine, without paid transport or CRM."""
import csv
import json
import re
import sys
from html import escape
from pathlib import Path

from control_analysis import ENGINE


INTRODUCTION = (
    'Мы провели бесплатный предварительный юридический анализ сайта вашей организации '
    'и обнаружили признаки несоответствия требованиям законодательства о персональных данных. '
    'По результатам анализа подготовили для вас индивидуальное коммерческое предложение.'
)


def recipient(name):
    name = ' '.join((name or '').split())
    name = re.sub(r'^(?:ООО|ПАО|НАО|ОАО|ЗАО|АО|ИП)\s+', '', name, flags=re.I)
    quotes = '«»„“”"'
    if name and name[:1] in quotes and name[-1:] in quotes:
        name = name[1:-1].strip()
    clinic = re.match(r'^клиника(?:\s+|$)', name, re.I)
    kind = 'клиники' if clinic else 'организации'
    if clinic:
        name = name[clinic.end():].strip()
        if name and name[:1] in quotes and name[-1:] in quotes:
            name = name[1:-1].strip()
    # Normalize paired internal quotes without changing the brand words.
    name = re.sub(r'[«„“"]([^«»„“”"]+)[»“”"]', r'«\1»', name)
    return 'Руководителю ' + kind + (' „' + name + '“' if name else '')


def personalize(page, name):
    from lexradar.client_proposal import PERSONALIZATION

    fields = list(PERSONALIZATION.finditer(page))
    if len(fields) != 3:
        raise ValueError('Unexpected proposal personalization fields')
    field = fields[1]
    page = page[:field.start(2)] + escape(recipient(name)) + page[field.end(2):]
    # Word splits the approved introduction across spans and line breaks.
    paragraphs = re.compile(r'<p\b[^>]*>(?:(?!</p>).)*?</p>', re.S | re.I)
    matches = [m for m in paragraphs.finditer(page) if re.search(
        r'В\s+рамках\s+программы\s+лояльности', re.sub(r'<[^>]+>', '', m[0]))]
    if len(matches) != 1:
        raise ValueError('Unexpected proposal introduction')
    match = matches[0]
    opening = re.match(r'<p\b[^>]*>', match[0], re.I)[0]
    style = re.search(r'<span\b[^>]*>', match[0], re.I)
    body = (style[0] + INTRODUCTION + '</span>') if style else INTRODUCTION
    return page[:match.start()] + opening + body + '</p>' + page[match.end():]


def main():
    sys.path.insert(0, str(ENGINE / 'src'))
    import lexradar.mvp_batch as batch
    with Path(sys.argv[1]).open(newline='', encoding='utf-8-sig') as stream:
        name = next(csv.DictReader(stream)).get('company_name', '')
    original = batch.render_client

    def render(*args, **kwargs):
        return personalize(original(*args, **kwargs), name)

    batch.render_client = render
    try:
        state = batch.run(Path(sys.argv[1]), Path(sys.argv[2]), mode='prepare', max_sites=1,
                max_ai_calls=0, allow_site_collection=True, ignore_robots=False)
    finally:
        batch.render_client = original
    row = state['rows'][0]
    print(json.dumps({'status': row['status'], 'reason': row.get('reason'),
                      'stages': row.get('stages', [])}, ensure_ascii=False), flush=True)
    return 0 if 'evidence_packet:SUCCESS' in row.get('stages', []) and row['status'] != 'ERROR' else 1


if __name__ == '__main__':
    raise SystemExit(main())
