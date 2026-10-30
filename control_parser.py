"""Fixed Parser v3 contract; diagnostics never launch a browser or contact a site."""
import ast
import json
import os
from pathlib import Path
import sys
import subprocess
from urllib.parse import urlsplit

from control_leads import load_leads
from control_results import read_file

SOURCE = Path('/home/golkonst/src/Parser-v3')
PARSER_TIMEOUT = 4 * 60 * 60
MAX_TARGET = 1000


class Parser:
    def __init__(self, source=SOURCE, python=None):
        self.source = Path(source).absolute()
        venv = self.source / '.venv/bin/python'
        self.python = str(python or (venv if venv.is_file() else sys.executable))
        self.browsers = Path.home() / '.cache/ms-playwright'

    def industries(self):
        # Read literal configuration without importing production code.
        try:
            tree = ast.parse(read_file(self.source, 'industries.py', 100_000).decode('utf-8'))
            config = next(ast.literal_eval(n.value) for n in tree.body
                          if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'INDUSTRIES' for t in n.targets))
            return {str(k): v['name'] for k, v in config.items() if v['status'] == 'ready'}
        except (OSError, ValueError, SyntaxError, StopIteration, KeyError, TypeError, AttributeError) as exc:
            raise ValueError('Конфигурация отраслей Parser v3 недоступна') from exc

    def validate(self, industry, target, name=''):
        if industry not in self.industries():
            raise ValueError('Выберите доступную отрасль Parser v3')
        if not isinstance(target, str) or not target.isascii() or not target.isdecimal() or not 1 <= int(target) <= MAX_TARGET:
            raise ValueError('N должен быть целым числом от 1 до 1000')
        if not isinstance(name, str) or len(name) > 100 or any(ord(c) < 32 for c in name):
            raise ValueError('Название: до 100 символов без управляющих символов')
        return dict(industry=industry, target=int(target), name=name.strip())

    def diagnostic(self, industry='1'):
        errors = []
        try:
            if industry not in self.industries():
                errors.append('Отрасль недоступна')
            for name in ('parser_run.py', 'engine.py', 'target_collection.py', 'db.py', 'requirements.txt'):
                read_file(self.source, name, 200_000)
        except (OSError, ValueError, SyntaxError, StopIteration, KeyError):
            errors.append('Исходники Parser v3 недоступны или конфигурация повреждена')
        probe = """import json
from pathlib import Path
from importlib.metadata import version
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    print(json.dumps(dict(version=version('playwright'), chromium=Path(p.chromium.executable_path).is_file())))
"""
        try:
            checked = subprocess.run([self.python, '-B', '-c', probe], stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10,
                                     env={'PATH': os.defpath, 'LANG': 'C.UTF-8',
                                          'PLAYWRIGHT_BROWSERS_PATH': str(self.browsers)})
            data = json.loads(checked.stdout) if checked.returncode == 0 else {}
            if data.get('version') != '1.63.0':
                errors.append('В Python Parser отсутствует проверенный Playwright 1.63.0')
            if not data.get('chromium'):
                errors.append('Chromium Playwright недоступен')
        except (OSError, ValueError, subprocess.SubprocessError):
            errors.append('Python / Playwright / Chromium недоступен для локальной проверки')
        if industry != '1' and not os.environ.get('DISPLAY'):
            errors.append('Orgpage запускает Chromium с окном: нужен доступный DISPLAY')
        return errors

    def command(self, params, directory):
        params = self.validate(params['industry'], str(params['target']), params['name'])
        return [self.python, '-B', '-u', str(self.source / 'parser_run.py'),
                '--industry', params['industry'], '--target', str(params['target']),
                '--max-cards', str(max(200, 10 * params['target'])), '--max-pages', '200',
                '--database', str(directory / 'parser.sqlite'), '--output', str(directory / 'parser.csv')]

    def result(self, directory, target):
        summary = json.loads(read_file(directory, 'parser.summary.json', 1_000_000))
        if not isinstance(summary, dict):
            raise ValueError('Некорректная сводка Parser v3')
        leads, warnings = load_leads(directory)
        usable = sum(bool(r['websites'] and (r['phone'] or r['email'])) for r in leads)
        result = {k: summary.get(k) for k in ('target_achieved', 'stop_reason', 'cards_reviewed', 'accepted', 'exported_total')}
        domains = {urlsplit(r['websites'][0]).hostname.lower().removeprefix('www.').rstrip('.').encode('idna').decode('ascii')
                   for r in leads if r['websites']}
        result.update(usable=usable, unique_sites=len(domains), warnings=warnings)
        if (warnings or any(r['source'] != 'parser.csv' for r in leads)
                or len(leads) != target or len(domains) != target or usable != target
                or summary.get('target_achieved') is not True
                or type(summary.get('exported_total')) is not int or summary['exported_total'] != usable):
            return result, 'Цель не подтверждена: проверьте число пригодных и причину остановки'
        return result, ''
