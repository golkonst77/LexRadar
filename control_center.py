"""Standalone, read-only LexRadar Control Center; Python standard library only."""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LexRadar — Центр управления</title>
<style>
:root { color-scheme: dark; font-family: system-ui, sans-serif; }
* { box-sizing: border-box; }
body { margin: 0; background: #0c111b; color: #e7edf7; line-height: 1.6; }
a { color: #a5c7ff; text-underline-offset: 4px; }
a:focus-visible { outline: 2px solid #8dbbff; outline-offset: 5px; }
header, main, footer { max-width: 1100px; margin: auto; padding: 24px; }
header { display: flex; align-items: center; justify-content: space-between;
  flex-wrap: wrap; gap: 20px; border-bottom: 1px solid #273248; }
.brand { font-size: 22px; font-weight: 750; letter-spacing: -.5px; }
.brand span { color: #84b4ff; }
nav { display: flex; gap: 20px; flex-wrap: wrap; }
nav a { font-size: 14px; }
main { padding-top: 56px; }
h1 { font-size: clamp(30px, 5vw, 48px); line-height: 1.2; margin: 16px 0; }
h2 { font-size: 20px; margin-top: 0; }
p { color: #aab8cc; }
.badge { display: inline-block; background: #15352e; color: #9ae2c2;
  padding: 5px 12px; border-radius: 20px; font-size: 13px; }
.lead { max-width: 680px; font-size: 18px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
  gap: 18px; margin: 36px 0; }
article { background: #141d2d; border: 1px solid #29364d; border-radius: 14px;
  padding: 24px; scroll-margin-top: 24px; }
.soon { color: #b4c5df; font-size: 13px; }
.status { border-top: 1px solid #273248; padding-top: 24px; }
footer { color: #8c9bb1; font-size: 13px; padding-bottom: 40px; }
</style>
</head>
<body>
<header>
<div class="brand">Lex<span>Radar</span> / Control Center</div>
<nav aria-label="Разделы центра управления">
<a href="#overview">Обзор</a><a href="#projects">Проекты</a>
<a href="#reports">Отчёты</a><a href="#settings">Настройки</a>
</nav>
</header>
<main id="overview">
<span class="badge">● Сервер работает</span>
<h1>Центр управления LexRadar</h1>
<p class="lead">Единая точка входа для будущей работы с проектами и отчётами.
Сейчас доступна стартовая страница и проверка состояния сервера.</p>
<div class="grid">
<article id="projects"><h2>Проекты</h2><p>Список проектов и их состояние.</p>
<span class="soon">Скоро · раздел в разработке</span></article>
<article id="reports"><h2>Отчёты</h2><p>Просмотр результатов и сохранённых материалов.</p>
<span class="soon">Скоро · раздел в разработке</span></article>
<article id="settings"><h2>Настройки</h2><p>Параметры центра управления.</p>
<span class="soon">Скоро · раздел в разработке</span></article>
</div>
<section class="status" aria-labelledby="status-title">
<h2 id="status-title">Состояние системы</h2>
<p>Локальный интерфейс запущен. Фоновые задачи отключены.</p>
<a href="/health">Проверка доступности</a> · <a href="/api/status">Состояние API</a>
</section>
</main>
<footer>LexRadar Control Center · Локальный режим · Анализ и рассылка не запускаются</footer>
</body>
</html>
"""


class ControlHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/":
            self.respond(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/health":
            self.respond_json(200, {"status": "ok"})
        elif path == "/api/status":
            self.respond_json(200, {
                "service": "LexRadar Control Center",
                "status": "ok",
                "mode": "read_only",
                "analysis_enabled": False,
                "mailing_enabled": False,
            })
        else:
            self.respond_json(404, {"error": "not_found"})

    def respond_json(self, status, payload):
        self.respond(status, json.dumps(payload).encode("utf-8"),
                     "application/json; charset=utf-8")

    def respond(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    with ThreadingHTTPServer(("127.0.0.1", args.port), ControlHandler) as server:
        print(f"LexRadar Control Center: http://127.0.0.1:{args.port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
