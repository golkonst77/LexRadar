"""Conservative, read-only network selection before CRM preparation."""
import re

from . import espo_export as espo


def main_domain(website):
    host = espo.website_domain(website)
    parts = host.split('.')
    # shortcut: unknown multi-level public suffixes stay separate; extend only with confirmed cases.
    if len(parts) > 2 and (parts[-2] in {'co', 'com', 'org', 'net', 'gov', 'edu'}
                           or '.'.join(parts[-2:]) in {'spb.ru', 'msk.ru', 'pp.ru', 'github.io', 'netlify.app', 'vercel.app'}):
        return host
    return '.'.join(parts[-2:])


def network_keys(row):
    keys = set()
    host = main_domain(row.get('website', ''))
    if host:
        keys.add('domain:' + host)
    name = ' '.join(row.get('company_name', '').casefold().replace('ё', 'е').split())
    if re.search(r'\bклиник[аи]?\s+(?:доктора\s+)?фомина\b', name):
        keys.add('name:клиника фомина')
    # Only an explicitly named network establishes cross-domain brand membership.
    match = re.search(r'\bсеть\s+(?:медицинских\s+)?(?:клиник|центров)\s+[«"“]([^»"”]{3,80})[»"”]', name)
    if match:
        keys.add('name:' + match[1])
    return keys


def contact_email(row):
    emails = list(dict.fromkeys(e.strip() for e in [row.get('email', '')] + row.get('emails', [])
                                if isinstance(e, str) and espo.ADDRESS.fullmatch(e.strip())
                                and len(e.strip()) <= 254 and len(e.strip().split('@')[0]) <= 64))
    def rank(email):
        local, host = email.casefold().split('@')
        return (local in {'info', 'contact', 'office', 'mail', 'hello', 'reception'}
                and host == main_domain(row.get('website', '')), host == main_domain(row.get('website', '')))
    return max(emails, key=rank, default='')


def select_networks(rows, state):
    """Return decisions keyed by stable manifest ID; journal attempts pin representatives."""
    candidates = {row['id']: dict(row) for row in rows}
    for key, entry in state.items():
        base = key.removesuffix(':draft')
        binding = entry.get('network') or entry.get('binding')
        if isinstance(binding, dict) and binding.get('website'):
            candidates.setdefault(base, dict(binding, id=base, eligible=False))
    groups = []
    for row in candidates.values():
        keys = network_keys(row)
        matching = [g for g in groups if keys & g[0]]
        group = (set(keys), [row])
        for old in matching:
            group[0].update(old[0])
            group[1].extend(old[1])
            groups.remove(old)
        groups.append(group)
    decisions = {}
    for keys, members in groups:
        def rank(row):
            entry = state.get(row['id'], {})
            draft = state.get(row['id'] + ':draft', {})
            email = contact_email(row)
            central = email and email.split('@')[0].casefold() in {'info', 'contact', 'office', 'mail', 'hello', 'reception'} and email.split('@')[1].casefold() == main_domain(row.get('website', ''))
            return (bool(entry or draft), bool(draft), row.get('eligible', True), bool(central), bool(email))
        representative = sorted(members, key=lambda r: r['id'])
        representative = max(representative, key=rank)
        for row in members:
            decisions[row['id']] = {'status': 'REPRESENTATIVE' if row['id'] == representative['id'] else 'DUPLICATE_NETWORK',
                                     'representative_id': representative['id'],
                                     'representative_name': representative['company_name'],
                                     'excluded': len(members) - 1, 'email': contact_email(row)}
    return decisions
