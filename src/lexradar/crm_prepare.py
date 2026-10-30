"""Prepare review materials; explicit web continuation can prepare unscheduled drafts."""
import argparse
import base64
import fcntl
import hashlib
import json
import re
import unicodedata
from collections import Counter
from datetime import date
from pathlib import Path

from . import espo_export as espo


def digest(data):
    return hashlib.sha256(data).hexdigest()


def materials(row, root):
    for field in ('dossier', 'letter_text'):
        if not isinstance(row.get(field), str) or not row[field].strip():
            raise espo.Hold('Missing ' + field)
    data, sha = espo.validate({**row, 'export_approved': True, 'email': '', 'phones': []}, root)
    if sha != row.get('pdf_sha256'):
        raise espo.Hold('PDF differs from recorded SHA-256')
    result = [{'path': str((root / row['pdf_path']).resolve()), 'sha256': sha,
               'data': data, 'pdf': True}]
    dossier = root / row['dossier']
    if not dossier.is_dir():
        result.append({'path': str(dossier.resolve()), 'error': 'Dossier directory missing'})
        return result
    for path in sorted(dossier.rglob('*')):
        if path.is_file():
            try:
                content = path.read_bytes()
                result.append({'path': str(path.resolve()), 'sha256': digest(content), 'data': content})
            except OSError:
                result.append({'path': str(path), 'error': 'Unreadable evidence file'})
    return result


def identity_text(value):
    value = ''.join(c for c in unicodedata.normalize('NFKC', value)
                    if unicodedata.category(c) != 'Cf' and c != '\u034f')
    value = re.sub(r'[«»“”„‟‹›]', '"', value)
    for full, short in (
        ('общество с ограниченной ответственностью', 'ООО'),
        ('публичное акционерное общество', 'ПАО'),
        ('непубличное акционерное общество', 'НАО'),
        ('закрытое акционерное общество', 'ЗАО'),
        ('открытое акционерное общество', 'ОАО'),
        ('акционерное общество', 'АО'),
    ):
        value = re.sub(r'\b' + r'\s+'.join(full.split()) + r'\b', short, value, flags=re.I)
    return re.sub(r'\b(ООО|АО|ПАО|НАО|ЗАО|ОАО|ГБУЗ|ГАУЗ)\s*(?=")', r'\1 ', value, flags=re.I)


def identity_name(value):
    return ' '.join(identity_text(value).replace('"', '').casefold().split())


class IdentityHold(espo.Hold):
    pass


def identify(row, root, entry):
    """Use saved same-host captures, never audit conclusions or brand similarity."""
    host = espo.website_domain(row['website'])
    dossier = root / row['dossier']
    path = dossier / 'collection.json'
    owner_names, site_operators, aliases = set(), set(), {}
    evidence, names, identifiers = [], {}, {'inn': set(), 'ogrn': set()}
    if path.exists():
        collection = json.loads(path.read_text())
        if espo.website_domain(collection.get('target_url')) != host:
            raise IdentityHold('Dossier target domain differs from manifest')
        pages = [{k: v for k, v in page.items() if k != 'reviewed_pdf'}
                 for page in collection.get('pages', [])]
        # Scanned statements are transcribed only after visual review, pinned to PDF bytes.
        reviewed = json.loads(Path(__file__).with_name('crm_identity_documents.json').read_text())
        for document in collection.get('documents', []):
            sha = document.get('reading', {}).get('source_sha256')
            statement = reviewed.get(sha)
            if (not statement or document.get('http_status') != 200 or
                    espo.website_domain(document.get('final_url')) != host):
                continue
            eid = document.get('evidence_id', '')
            if not re.fullmatch(r'document-[0-9]+', eid):
                raise IdentityHold('Invalid saved document evidence ID')
            pdf = dossier / 'artifacts' / (eid + '.pdf')
            if digest(pdf.read_bytes()) != sha:
                raise IdentityHold('Saved document hash mismatch')
            if host != statement['host']:
                raise IdentityHold('Reviewed document domain mismatch')
            pages.append({'final_url': document['final_url'], 'http_status': 200,
                          'reviewed_pdf': str(pdf), 'text': statement['quote'],
                          'page': statement['page'], 'section': statement['section'],
                          'reading': {'source_sha256': sha}})
        for page in pages:
            url = page.get('final_url', '')
            if page.get('http_status') != 200 or espo.website_domain(url) != host:
                continue
            eid = page.get('evidence_id', '')
            if page.get('reviewed_pdf'):
                original = Path(page['reviewed_pdf'])
            else:
                if not re.fullmatch(r'page-[0-9]+', eid):
                    raise IdentityHold('Invalid saved page evidence ID')
                original = dossier / 'artifacts' / (eid + '.html')
                text_file = dossier / 'artifacts' / (eid + '.txt')
                if (digest(original.read_bytes()) != page.get('reading', {}).get('source_sha256') or
                        text_file.read_text() != page.get('text')):
                    raise IdentityHold('Saved page hash/text mismatch: ' + url)
            text = identity_text(page['text'])
            # Only an explicit parenthesized short legal name establishes an alias.
            for full, short in re.findall(
                    r'(ООО\s*"[^"\n]{1,150}")\s*\(\s*(ООО\s*"[^"\n]{1,150}")', text, re.I):
                aliases[identity_name(short)] = identity_name(full)
            matches = list(re.finditer(r'\b(?:ООО|АО|ПАО|НАО|ЗАО|ОАО|ГБУЗ|ГАУЗ)\s*"[^"\n]{1,150}"', text, re.I))
            contractor_list = False
            candidate_text = []
            end = 0
            for index, match in enumerate(matches):
                name = match.group()
                before = text[end:match.start()]
                after_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
                after = text[match.end():after_end]
                # A processor list can span several companies and their requisites.
                if re.search(r'лиц.{0,100}по поручению|по поручению.{0,100}лиц', before[-500:], re.I):
                    contractor_list = True
                elif re.search(r'оператор вправе|общая информация о медицинской организации|владелец сайта', before, re.I):
                    contractor_list = False
                local_before = before[-240:]
                service = re.search(
                    r'(?:аналитическ|техническ|сторонн).{0,60}сервис|'
                    r'сервис.{0,60}(?:аналитик|предоставля|разработан)|'
                    r'разработчик сайта|хостинг.провайдер', local_before, re.I)
                service = service or re.search(
                    r'^\s*[,—–-]?\s*(?:предоставляет.{0,80}(?:сервис|аналитик)|'
                    r'разработчик сайта|провайдер|оператор сервиса)', after, re.I)
                service = service or re.search(
                    r'веб[ -]анал[иі]з|веб[ -]аналитик|сервис.{0,100}реклам|'
                    r'пиксель|рекламодател|подрядчик|полученные данные хранятся у',
                    before[-600:], re.I)
                service = service or re.search(
                    r'данными со следующими организациями|'
                    r'о страховых медицинских организациях|'
                    r'участие в (?:мастер-классе|семинаре|конгрессе)[^.;]{0,180}\(\s*$',
                    before[-400:], re.I)
                if re.search(r'(?:владелец сайта|оператор сайта|©)[^.;]{0,80}$', local_before, re.I):
                    contractor_list = False
                    service = None
                if re.search(r'лицензи[яи].{0,80}медицинск|оказани[ея].{0,60}медицинск', after[:240], re.I):
                    contractor_list = False
                    service = None
                excluded = contractor_list or bool(service)
                context = local_before[-160:] + name + after[:160]
                role = 'contractor' if excluded else 'identity_candidate'
                if not excluded:
                    names[identity_name(name)] = name
                    if (re.search(r'оператор[^.;]{0,80}$', local_before, re.I) and
                            any(espo.website_domain(u.rstrip('.,);')) == host
                                for u in re.findall(r'https?://[^\s<>]+', text))):
                        site_operators.add(identity_name(name))
                    candidate_text.append((before if index == 0 else '') + name + after)
                    if re.search(r'©|оператор|администраци|медицинской организации|веб[–-]сервис|принадлеж|собственност', context, re.I):
                        owner_names.add(identity_name(name))
                evidence.append({'url': url, 'path': str(original.resolve()),
                                 'sha256': digest(original.read_bytes()), 'name': name, 'role': role,
                                 'quote': local_before[-80:] + name + after[:160],
                                 **({'page': page['page'], 'section': page['section'],
                                     'transcription': page['text']} if page.get('reviewed_pdf') else {})})
                end = match.end()
            for field, pattern in {'inn': r'ИНН\s*[:№]?\s*(\d{10}(?:\d{2})?)(?!\d)',
                                   'ogrn': r'ОГРН(?:ИП)?\s*[:№]?\s*(\d{13}(?:\d{2})?)(?!\d)'}.items():
                identifiers[field].update(re.findall(pattern, ''.join(candidate_text) if matches else text, re.I))
    for short, full in aliases.items():
        if short != full and short in names and full in names:
            del names[short]
            if short in owner_names:
                owner_names.add(full)
            if short in site_operators:
                site_operators.add(full)
    if len(names) > 1 or any(len(values) > 1 for values in identifiers.values()):
        raise IdentityHold('Multiple legal entities/requisites in saved site: ' + '; '.join(names.values()))
    for field, values in identifiers.items():
        if row.get(field) and values and str(row[field]) not in values:
            raise IdentityHold('Manifest ' + field + ' conflicts with saved site')
    requested = identity_name(row['company_name'])
    if names:
        legal, name = next(iter(names.items()))
        if legal not in owner_names:
            raise IdentityHold('Legal name mentioned without site owner/operator evidence: ' + name)
        core = re.sub(r'^(ооо|ао|пао|нао|зао|оао|гбуз|гауз) ', '', legal)
        documented_operator = (legal in site_operators and
                               all(len(v) == 1 for v in identifiers.values()) and
                               not re.search(r'^(?:ооо|ао|пао|нао|зао|оао|гбуз|гауз)\b|филиал|на улице|на ул\.|на проспекте', requested))
        if requested not in (legal, core) and not documented_operator:
            raise IdentityHold('Manifest name/branch not linked to legal entity: ' + name)
        return {'verified': True, 'method': 'saved_site', 'company_name': name, 'legal_name': name,
                'brand_name': row['company_name'],
                'evidence': evidence, 'identifiers': {k: sorted(v) for k, v in identifiers.items()}}
    previous = entry.get('result', {})
    if previous.get('identity_verified') is True and entry.get('account_id'):
        if previous.get('identity', {}).get('method') == 'manual':
            return previous['identity']
        return {'verified': True, 'method': 'saved_verified_binding',
                'company_name': row['company_name'], 'account_id': entry['account_id']}
    if row.get('identity_verified') is True:
        return {'verified': True, 'method': 'manual', 'company_name': row['company_name']}
    raise IdentityHold('No legal identity evidence on saved same-domain pages')


class Prepare(espo.Exporter):
    def ensure(self, entry, step, kind, payload, field='name'):
        if kind != 'Attachment':
            return super().ensure(entry, step, kind, payload, field)
        saved = entry['steps'].get(step)
        if saved:
            if not saved.get('id'):
                raise espo.Hold('Uncertain Attachment POST; reconcile before retry')
            record = self.api.get(kind, saved['id'])
            if any(record.get(k) != v for k, v in payload.items() if k != 'file'):
                raise espo.Hold('Attachment verification mismatch')
            return record['id']
        entry['steps'][step] = {'pending': True}
        espo.save(self.journal, self.state)
        response = self.api.request('POST', kind, payload)
        entry['steps'][step] = {'id': response['id']}
        espo.save(self.journal, self.state)
        return self.ensure(entry, step, kind, payload, field)

    def ensure_note(self, entry, step, payload):
        if entry.get('note_skipped'):
            raise espo.Hold(entry['note_skipped'])
        saved = entry['steps'].get(step)
        attempted = any(key.startswith('note-') or key == 'note' for key in entry['steps'])
        if saved and saved.get('id'):
            record = self.api.get('Note', saved['id'])
        else:
            found = self.api.find('Note', 'post', payload['post'].split('\n', 1)[0], operator='contains')
            if len(found) > 1:
                raise espo.Hold('Ambiguous Note recovery')
            if found:
                record = self.api.get('Note', found[0]['id'])
            elif attempted:
                raise espo.Hold('Uncertain/previous Note attempt; no repeat POST')
            else:
                entry['steps'][step] = {'pending': True}
                espo.save(self.journal, self.state)
                response = self.api.request('POST', 'Note', payload)
                entry['steps'][step]['id'] = response['id']
                espo.save(self.journal, self.state)
                record = self.api.get('Note', response['id'])
        if any(record.get(k) != v for k, v in payload.items()):
            raise espo.Hold('Note verification mismatch')
        entry['steps'][step] = {'id': record['id']}
        espo.save(self.journal, self.state)
        return record['id']

    def account(self, row, entry, marker, *, preview=False):
        host = espo.website_domain(row['website'])
        candidates = self.api.find('Account', 'website', host, operator='contains')
        candidates = [c for c in candidates if espo.website_domain(c.get('website')) == host]
        candidates += self.api.find('Account', 'name', row['company_name'])
        saved_id = entry.get('account_id') or entry.get('steps', {}).get('account', {}).get('id')
        if saved_id:
            candidates.append(self.api.get('Account', saved_id))
        candidates = {c['id']: c for c in candidates}
        if len(candidates) > 1:
            raise IdentityHold('Ambiguous Account; reconcile verified identity')
        if candidates:
            account = self.api.get('Account', next(iter(candidates)))
            if identity_name(account.get('name', '')) != identity_name(row['company_name']) or espo.website_domain(account.get('website')) != host:
                raise IdentityHold('Account identity conflict; no duplicate or reassignment allowed')
            account_id = account['id']
        else:
            if preview:
                return None
            account_id = self.ensure(entry, 'account', 'Account', {
                'name': row['company_name'], 'website': row['website'], 'description': marker,
                **self.contacts({'phones': []}, {})}, 'description')
        if entry.get('account_id', account_id) != account_id:
            raise espo.Hold('Saved Account binding changed')
        if preview:
            return account_id
        entry['account_id'] = account_id
        espo.save(self.journal, self.state)
        return account_id

    def document(self, entry, account, marker, item, definitions):
        fields = definitions.get('Document', {}).get('fields', {})
        if any(k not in fields for k in ('name', 'file', 'publishDate', 'accounts')):
            raise espo.Hold('Document schema unsupported; material retained locally')
        step = 'material-' + digest(item['path'].encode())
        name = marker + '-' + digest((item['path'] + item['sha256']).encode())[:20]
        existing = self.api.request('GET', 'Account/' + account + '/documents')
        if not isinstance(existing.get('list'), list) or existing.get('total', len(existing['list'])) > len(existing['list']):
            raise espo.Hold('Incomplete Account documents search')
        matches = [doc for doc in existing['list'] if doc.get('name') == name]
        if len(matches) > 1:
            raise espo.Hold('Ambiguous material Document')
        saved = entry['steps'].get(step, {})
        if matches or saved.get('id'):
            document = self.api.get('Document', saved.get('id') or matches[0]['id'])
            links = self.api.request('GET', 'Document/' + document['id'] + '/accounts')
            if (document.get('name') != name or
                    {r.get('id') for r in links.get('list', [])} != {account} or
                    digest(self.api.file(document.get('fileId'))) != item['sha256']):
                raise espo.Hold('Existing material binding/SHA-256 mismatch')
            attachment = document['fileId']
            saved_file = entry['steps'].get(step + '-file', {}).get('id')
            if saved_file and saved_file != attachment:
                raise espo.Hold('Saved Attachment binding changed')
            entry['steps'][step] = {'id': document['id']}
            entry['steps'][step + '-file'] = {'id': attachment}
            espo.save(self.journal, self.state)
            return {'document_id': document['id'], 'attachment_id': attachment}
        mime = 'application/pdf'
        attachment = self.ensure(entry, step + '-file', 'Attachment', {
            'name': name + '-' + Path(item['path']).name, 'type': mime,
            'role': 'Attachment', 'relatedType': 'Document', 'field': 'file',
            'file': 'data:' + mime + ';base64,' + base64.b64encode(item['data']).decode()})
        document = self.ensure(entry, step, 'Document', {
            'name': name, 'fileId': attachment, 'publishDate': entry['publish_date'],
            'accountsIds': [account]})
        links = self.api.request('GET', 'Document/' + document + '/accounts')
        if {r.get('id') for r in links.get('list', [])} != {account}:
            raise espo.Hold('Document belongs to different/ambiguous Account; unchanged')
        if digest(self.api.file(attachment)) != item['sha256']:
            raise espo.Hold('Uploaded material SHA-256 mismatch')
        return {'document_id': document, 'attachment_id': attachment}


def run(rows, root, journal, *, confirm=False, api=None, check_crm=False, accounts_only=False, prepare_drafts=False):
    results = []
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(journal.read_text()) if journal.exists() else {}
        for row in rows:
            result = {'id': row.get('id') if isinstance(row, dict) else None, 'status': 'HOLD', 'errors': [], 'warnings': [], 'materials': []}
            try:
                if not isinstance(row, dict):
                    raise espo.Hold('Row must be an object')
                if sum(isinstance(r, dict) and r.get('id') == row.get('id') for r in rows) != 1:
                    raise espo.Hold('Duplicate manifest id')
                items = materials(row, root)
                result['materials'] = [{k: v for k, v in item.items() if k != 'data'} for item in items]
                result['identity_verified'] = row.get('identity_verified') is True
                binding = {'host': espo.website_domain(row['website']), 'company': row['company_name'],
                           'materials': result['materials'], 'letter': row['letter_text']}
                key = digest(json.dumps(binding, sort_keys=True, ensure_ascii=False).encode())
                entry = state.get(row['id'], {})
                if entry and entry.get('binding') != key:
                    raise espo.Hold('Manifest changed under saved ID; reconcile journal')
                try:
                    result['identity'] = identify(row, root, entry)
                except (OSError, ValueError, KeyError, TypeError):
                    raise IdentityHold('Saved identity evidence missing, unreadable or malformed') from None
                result['identity_verified'] = True
                if not confirm:
                    if check_crm:
                        if api is None:
                            api = espo.API()
                        worker = Prepare(api, journal, state, {'Account': {'fields': {}}})
                        result['account_id'] = worker.account(
                            {**row, 'company_name': result['identity']['company_name']},
                            entry, '', preview=True)
                        result['crm_identity_checked'] = True
                    result['status'] = 'PREVIEW'
                    continue
                if (accounts_only or prepare_drafts) and 'draft_contact' in entry:
                    if entry['draft_contact'] != row.get('email', ''):
                        raise espo.Hold('Confirmed email changed; reconcile journal before retry')
                if prepare_drafts:
                    previous = entry.get('result', {})
                    saved_draft = state.get(row['id'] + ':draft', {})
                    saved_binding = saved_draft.get('binding', {})
                    # Only the original pre-write binding failure proves where export stopped.
                    binding_failure = (previous.get('status') == 'HOLD'
                                       and previous.get('identity_verified') is True
                                       and entry.get('account_id')
                                       and previous.get('account_id') == entry.get('account_id')
                                       and previous.get('errors') == [{'error': 'Account binding changed'}]
                                       and saved_draft.get('account_id') == entry.get('account_id')
                                       and entry.get('steps') == {}
                                       and saved_draft.get('steps') == {}
                                       and saved_draft.get('sha256') == items[0]['sha256']
                                       and isinstance(saved_binding, dict)
                                       and identity_name(saved_binding.get('company_name', ''))
                                       == identity_name(result['identity']['company_name'])
                                       and all(saved_binding.get(k) == row.get(k, '')
                                               for k in ('website', 'email')))
                    resumable = saved_draft.get('resume_protocol') == 1
                    if entry.get('draft_error') and not (binding_failure or resumable):
                        raise espo.Hold('Previous draft failure; outcome unknown. Сверьте Account, Document/PDF и Email в CRM с журналом; пустые steps не разрешают повтор POST. Не сбрасывайте draft_error.')
                    if (saved_draft and not (resumable or binding_failure)
                            and previous.get('status') not in ('DRAFT_VERIFIED', 'DOCUMENT_VERIFIED_NO_DRAFT')):
                        raise espo.Hold('Legacy draft attempt has no durable recovery protocol; сверьте Document, Email и вложения в CRM, повтор POST заблокирован')
                    if (previous.get('status') not in ('ACCOUNT_VERIFIED', 'DRAFT_VERIFIED', 'DOCUMENT_VERIFIED_NO_DRAFT') and not (binding_failure or resumable)) or not entry.get('account_id'):
                        raise espo.Hold('ACCOUNT_VERIFIED required before preparing draft')
                    if api is None:
                        api = espo.API()
                    definitions = espo.check_schema(api)
                    worker = Prepare(api, journal, state, definitions)
                    verified_row = {**row, 'company_name': result['identity']['company_name']}
                    account = worker.account(verified_row, entry, '', preview=True)
                    # The exporter requires the exact CRM spelling after our normalized identity check.
                    account_record = api.get('Account', account)
                    if (identity_name(account_record.get('name', '')) != identity_name(verified_row['company_name'])
                            or espo.website_domain(account_record.get('website')) != binding['host']):
                        raise IdentityHold('Account identity conflict; no duplicate or reassignment allowed')
                    verified_row['company_name'] = account_record['name']
                    result['account_id'] = account
                    # Reuse the existing exporter and its durable steps in the same locked journal.
                    draft_id = row['id'] + ':draft'
                    draft = state.setdefault(draft_id, {'sha256': items[0]['sha256'], 'steps': {},
                                                       'account_id': account})
                    if draft.get('account_id') != account:
                        raise espo.Hold('Draft Account binding changed')
                    export_row = {**verified_row, 'id': draft_id, 'export_approved': True}
                    espo.validate(export_row, root)
                    saved_binding = draft.get('binding')
                    export_binding = {k: export_row.get(k, '') for k in ('company_name', 'website', 'email')}
                    if (binding_failure and saved_binding
                            and identity_name(saved_binding.get('company_name', '')) == identity_name(export_binding['company_name'])
                            and all(saved_binding.get(k) == export_binding[k] for k in ('website', 'email'))):
                        draft['binding'] = export_binding
                    material_step = 'material-' + digest(items[0]['path'].encode())
                    existing_document = entry.get('steps', {}).get(material_step, {}).get('id')
                    if existing_document:
                        export_row['existing_document_id'] = existing_document
                    template = espo.template_at(api, None)
                    draft['resume_protocol'] = 1
                    espo.save(journal, state)
                    result.update(espo.Exporter(api, journal, state, definitions).export(
                        export_row, items[0]['data'], items[0]['sha256'], template))
                    entry.pop('draft_error', None)
                    continue
                if api is None:
                    api = espo.API()
                definitions = api.request('GET', 'Metadata').get('entityDefs', {})
                for kind, required in {'Account': ('name', 'website', 'description')}.items():
                    if any(k not in definitions.get(kind, {}).get('fields', {}) for k in required):
                        raise espo.Hold('Required CRM schema unavailable: ' + kind)
                entry = state.setdefault(row['id'], {'binding': key, 'steps': {},
                                                    'publish_date': date.today().isoformat()})
                worker = Prepare(api, journal, state, definitions)
                marker = 'LexRadar-REVIEW-' + digest((row['id'] + key).encode())
                account = worker.account({**row, 'company_name': result['identity']['company_name']}, entry, marker)
                result['account_id'] = account
                if accounts_only:
                    entry.setdefault('draft_contact', row.get('email', ''))
                    result['status'] = 'ACCOUNT_VERIFIED'
                    continue
                for item, outcome in zip(items, result['materials']):
                    if not item.get('pdf'):
                        outcome['storage'] = 'server_only'
                        if 'error' in item:
                            result['warnings'].append({'path': item['path'], 'error': item['error']})
                        continue
                    try:
                        skipped = entry.get('skipped', {}).get(item['path'])
                        if skipped and skipped != 'CRM HTTP 403: GET Attachment; inspect CRM before retry':
                            raise espo.Hold(skipped)
                        if 'error' in item:
                            raise espo.Hold(item['error'])
                        outcome.update(worker.document(entry, account, marker, item, definitions))
                        entry.get('skipped', {}).pop(item['path'], None)
                        espo.save(journal, state)
                    except espo.Hold as exc:
                        outcome['error'] = str(exc)
                        entry.setdefault('skipped', {})[item['path']] = str(exc)
                        espo.save(journal, state)
                        result['errors'].append({'path': item['path'], 'error': str(exc)})
                if not result['materials'][0].get('document_id'):
                    raise espo.Hold('Required PDF Document not verified')
                post = marker + '\nPENDING_REVIEW\nНЕ ОТПРАВЛЯТЬ. Требуется проверка.\n' + row['letter_text']
                post += '\n\nЛокальные доказательства и результаты переноса:\n' + json.dumps(result['materials'], ensure_ascii=False, indent=2)
                # Each immutable outcome gets its own marker, so a partial retry cannot duplicate a Note.
                note_step = 'note-' + digest(post.encode())
                post = marker + '-' + digest(post.encode()) + '\n' + post
                try:
                    if any(k not in definitions.get('Note', {}).get('fields', {})
                           for k in ('type', 'post', 'parent')):
                        result['warnings'].append({'error': 'Note skipped: CRM schema unavailable'})
                    else:
                        result['note_id'] = worker.ensure_note(entry, note_step, {
                            'type': 'Post', 'parentType': 'Account', 'parentId': account, 'post': post})
                except (espo.Hold, OSError, ValueError, KeyError, TypeError) as exc:
                    message = str(exc) if isinstance(exc, espo.Hold) else 'Local/API failure; inspect journal'
                    entry['note_skipped'] = message
                    espo.save(journal, state)
                    result['warnings'].append({'error': 'Note skipped: ' + message})
                result['status'] = 'PENDING_REVIEW' if result['materials'][0].get('document_id') else 'HOLD'
                entry['result'] = result
                espo.save(journal, state)
            except IdentityHold as exc:
                result['status'] = 'HOLD_IDENTITY'
                result['identity_verified'] = False
                result['errors'].append({'error': str(exc)})
            except espo.Hold as exc:
                result['errors'].append({'error': str(exc)})
            except (OSError, ValueError, KeyError, TypeError):
                result['errors'].append({'error': 'Local/API failure; inspect journal before retry'})
            finally:
                if confirm and isinstance(row, dict) and row.get('id') in state:
                    if prepare_drafts:
                        draft = state.get(row['id'] + ':draft', {})
                        if draft.get('account_id'):
                            result.setdefault('account_id', draft['account_id'])
                        for step, field in (('document', 'document_id'), ('email', 'email_id')):
                            saved_id = draft.get('steps', {}).get(step, {}).get('id')
                            if saved_id:
                                result.setdefault(field, saved_id)
                        if result['errors']:
                            state[row['id']]['draft_error'] = True
                            state[row['id']].setdefault('draft_failure', result['errors'])
                    state[row['id']]['result'] = result
                    espo.save(journal, state)
                results.append(result)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--check-crm', action='store_true', help='Read-only Account identity check during preview')
    parser.add_argument('--confirm-prepare-crm', action='store_true')
    parser.add_argument('--journal', type=Path, default=Path.home() / '.local/state/lexradar/crm-prepare.json')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        results = run(espo.read_manifest(args.manifest), args.manifest.resolve().parent,
                      args.journal, confirm=args.confirm_prepare_crm, check_crm=args.check_crm)
        espo.save(args.output, results)
        print(json.dumps({'mode': 'CRM_PREPARE_ONLY', 'rows': len(results),
                          'statuses': dict(Counter(r['status'] for r in results)),
                          'automatic_confirmations': sum(r.get('identity', {}).get('method') in ('saved_site', 'saved_verified_binding') for r in results),
                          'identity_requires_manual': sum(r['status'] == 'HOLD_IDENTITY' for r in results),
                          'manual_intervention': sum(r['status'].startswith('HOLD') for r in results),
                          'hold_reasons': dict(Counter(e['error'] for r in results if r['status'].startswith('HOLD') for e in r['errors'])),
                          'output': str(args.output)}, ensure_ascii=False))
        return int(any(r['status'].startswith('HOLD') for r in results))
    except (espo.Hold, OSError, ValueError):
        print('Invalid manifest/journal or output unavailable')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
