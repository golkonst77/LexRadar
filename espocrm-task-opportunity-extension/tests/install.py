"""Real installer in a temporary Espo-shaped directory, no CRM bootstrap execution."""
import json
import os
from pathlib import Path
import subprocess
import tempfile

base = Path(__file__).resolve().parents[1]
php = os.environ.get('PHP_BIN', 'php')
with tempfile.TemporaryDirectory(prefix='lr-install-') as tmp:
    root = Path(tmp) / 'crm'
    (root / 'data').mkdir(parents=True)
    (root / 'bootstrap.php').write_text('<?php throw new Exception("Must not bootstrap CRM");')
    (root / 'data/config.php').write_text('<?php return ["version" => "10.0.6"];')
    rel = 'custom/Espo/Custom/Resources/metadata/entityDefs/Opportunity.json'
    target = root / rel
    target.parent.mkdir(parents=True)
    original = '{"fields":{"cExisting":{"type":"varchar"},"stage":{"options":["Existing"]}},"customKey":true}\n'
    target.write_text(original)
    backup = Path(tmp) / 'backup'
    def run(*args, ok=True):
        r = subprocess.run([php, '-n', str(base/'install.php'), f'--root={root}', *args], capture_output=True, text=True)
        assert (r.returncode == 0) == ok, r.stdout + r.stderr
        return r.stdout
    run()
    assert target.read_text() == original
    run('--apply', f'--backup={backup}')
    merged = json.loads(target.read_text())
    assert merged['fields']['stage']['options'] == ['Existing']
    assert merged['fields']['cExisting']['type'] == 'varchar' and merged['customKey']
    assert merged['indexes']['cLrSourceTaskUnique']['unique']
    assert 'Already installed' in run('--apply', f'--backup={backup}')
    installed = target.read_text()
    disable_backup = Path(tmp) / 'disable'
    run('--disable', '--apply', f'--backup={disable_backup}')
    assert target.read_text() == installed
    routes = json.loads((root/'custom/Espo/Custom/Resources/routes.json').read_text())
    assert routes == []
    run('--apply', f'--backup={Path(tmp)/"reenable"}')
    assert target.read_text() == installed
    target.write_text(installed+' ')
    run('--rollback', f'--backup={backup}', '--apply', ok=False)
    target.write_text(installed)
    run('--rollback', f'--backup={backup}', '--apply')
    assert target.read_text() == original
    assert not (root/'client/custom/src/lexradar/task-opportunity.js').exists()
    # Conflicting field must fail before any payload is installed.
    target.write_text('{"fields":{"cLrSourceTask":{"type":"varchar"}}}')
    run('--apply', f'--backup={Path(tmp)/"other"}', ok=False)
    assert not (root/'client/custom/src/lexradar/task-opportunity.js').exists()
for file in (base/'patches').rglob('*.json'):
    json.loads(file.read_text())
print('Installer: plan, preservation, repeat, conflict, rollback and JSON: OK')
