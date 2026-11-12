import io, json, tempfile
from pathlib import Path
from types import SimpleNamespace
from email.message import Message
from urllib.parse import urlencode
from unittest.mock import patch
from control_mvp import MVPHandler
from control_mailing import Mailing

with tempfile.TemporaryDirectory() as tmp, patch('control_mailing.espo.API', side_effect=AssertionError('CRM forbidden')):
    directory = Path(tmp)
    queue = Mailing(directory)
    protected = {'mailing.json': json.dumps({'x': {'status': 'SCHEDULED', 'slot': '2026-12-01 09:00:00', 'sendAt': '2026-12-01 09:00:00'}, 'y': {'status': 'SENT'}}), 'mailing-reschedule.json': '{"untouched":true}'}
    for name, content in protected.items():
        (directory / name).write_text(content)
    h = object.__new__(MVPHandler)
    h.server = SimpleNamespace(mailing=queue, csrf_token='token')
    h.path = '/mailing'
    h.valid_local_request = lambda: True
    h.origin_rejection = lambda: None
    h.html = lambda body, status=200: results.append((status, body))
    h.reject_post = lambda *args: results.append(args)
    results = []
    h.do_GET()
    assert results[-1][0] == 200 and 'Сохранить расписание' in results[-1][1] and 'value="15"' in results[-1][1]
    def post(**changes):
        fields = dict(csrf='token', interval_minutes='20', weekdays=['2', '7'], start_time='10:07', end_time='16:32', timezone='Europe/Moscow')
        fields.update(changes)
        raw = urlencode(fields, doseq=True).encode()
        h.headers = Message()
        h.headers['Content-Length'] = str(len(raw))
        h.headers['Content-Type'] = 'application/x-www-form-urlencoded'
        h.rfile = io.BytesIO(raw)
        h.do_POST()
    post()
    assert results[-1][0] == 200 and 'Расписание сохранено' in results[-1][1]
    saved = queue.load_settings()
    assert saved['weekdays'] == [2, 7] and saved['interval_minutes'] == 20
    h.do_GET()
    assert 'value="20"' in results[-1][1] and '10:07–16:32' in results[-1][1]
    for changes in (dict(interval_minutes='4'), dict(weekdays=[]), dict(end_time='09:00'), dict(start_time='9:00'), dict(timezone='UTC')):
        post(**changes)
        assert results[-1][0] == 400
        assert queue.load_settings() == saved
    post(csrf='bad')
    assert results[-1][0] == 403
    for name, content in protected.items():
        assert (directory / name).read_text() == content
print('GET/POST, validation, CSRF, no CRM, protected journals: OK')
