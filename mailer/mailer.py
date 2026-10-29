#!/usr/bin/env python3
"""LexRadar EspoCRM mail queue preview. Read-only: no messages are sent or changed."""
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
IDS = [("Example company", "example-email")]

def next_slot(after, config, used):
    tz = ZoneInfo(config['timezone'])
    d = after.astimezone(tz)
    interval = int(config['interval_minutes'])
    start = int(config['start_hour'])
    end = int(config['end_hour'])
    for _ in range(30):
        if d.weekday() in config['working_days']:
            for minute in range(start * 60, end * 60, interval):
                slot = datetime.combine(d.date(), time(minute // 60, minute % 60), tzinfo=tz)
                if slot > after.astimezone(tz) and slot.isoformat() not in used:
                    used.add(slot.isoformat())
                    return slot
        d = datetime.combine(d.date() + timedelta(days=1), time(0), tzinfo=tz)
    raise RuntimeError('Нет свободного слота в ближайшие 30 дней')

def main():
    config = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
    if not config.get('dry_run', False) or config.get('enabled', False):
        sys.exit('ОТКАЗ: этот модуль работает только с dry_run=true и enabled=false')
    if int(config['interval_minutes']) < 12:
        sys.exit('ОТКАЗ: интервал должен быть не менее 12 минут')
    key = os.environ.get('ESPO_API_KEY')
    if not key:
        sys.exit('Нет ESPO_API_KEY. Выполните: set -a; source ~/.config/lexradar/espo.env; set +a')
    api_base = config['espo_api'].rstrip('/') + '/'
    now = datetime.now(ZoneInfo(config['timezone']))
    print('LexRadar — предварительный просмотр, ничего не отправляется')
    print('Время:', now.strftime('%d.%m.%Y %H:%M %Z'))
    print('График: пн–пт, 09:00–18:00 МСК, до 5 писем/час')
    used_slots = set()
    seen_addresses = set()
    counts = {'READY': 0, 'HOLD': 0, 'REVIEW': 0, 'SKIP': 0}
    for name, eid in IDS:
        try:
            req = urllib.request.Request(api_base + 'Email/' + eid, headers={'X-Api-Key': key})
            with urllib.request.urlopen(req, timeout=20) as response:
                item = json.load(response)
            status = item.get('status')
            recipient = (item.get('to') or '').strip().lower()
            files = list((item.get('attachmentsNames') or {}).values())
            pdfs = [f for f in files if isinstance(f, str) and f.lower().endswith('.pdf')]
            if status != 'Draft' or item.get('sendAt'):
                result, reason = 'SKIP', 'уже отправлено/запланировано или не черновик'
            elif not recipient or '@' not in recipient:
                result, reason = 'HOLD', 'нет адреса получателя'
            elif not pdfs:
                result, reason = 'HOLD', 'нет PDF'
            elif recipient in seen_addresses:
                result, reason = 'REVIEW', 'дублирующийся адрес в выборке'
            else:
                result, reason = 'READY', 'технически готово, разрешение на отправку НЕ выдано'
                seen_addresses.add(recipient)
            counts[result] += 1
            print(f'\n{name}: {result} | {recipient or "нет email"} | {", ".join(pdfs) or "нет PDF"}')
            if result == 'READY':
                slot = next_slot(now, config, used_slots)
                print('  Возможный слот:', slot.strftime('%d.%m.%Y %H:%M МСК'))
            print('  ', reason)
        except Exception as exc:
            counts['REVIEW'] += 1
            print(f'\n{name}: REVIEW | ошибка чтения: {exc}')
    print('\nИТОГО:', ', '.join(f'{k}={v}' for k, v in counts.items()))
    print('ОТПРАВКА ВЫКЛЮЧЕНА. Изменений в EspoCRM нет.')

if __name__ == '__main__':
    main()
