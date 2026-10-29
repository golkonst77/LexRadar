# Prepare an authorized small run now — 12 October 2026

This is an execution checklist, not a deployment. No live request is performed by
this PR. Never disable a mandatory proxy or bypass its destination policy.
Existing Collector needs direct public egress: an allowlisted mandatory HTTP proxy
alone does not work with its DNS-pinned transport.

## Required parameters / information from user

- A permitted Linux execution machine/session, Python 3.12+, and public DNS plus
  direct TCP 80/443 access authorized by the machine/network owner. If only a
  mandatory proxy is available, stop; do not unset it. No VPS/service deployment needed.
- How the user can execute commands there, or an explicitly authorized access method.
  Do not paste passwords/API keys into chat or the repository.
- 3–5 allowed public smoke URLs; input file path and a private
  writable output directory. Confirm permission for limited automated GET collection.
- Start with 1 worker, approximately 4 GB RAM and disk free for bounded attempts;
  measure usage before raising to 2–4 workers. For 1000 jobs provision the documented
  per-site worst-case disk budget or enforce an operator stop/monitoring procedure.
- Explicit normative control date and exact SourceRegistry path only if that stage
  is enabled. Research-base JSON is not the SourceRegistry CLI format.
- LLM budget $0 for this release path. No keys required.

## Installation and offline verification

After permission to use that execution environment, these commands install/run
locally. They do not open real sites. Reviewing/merging/deploying remains separate.

```bash
git clone https://github.com/golkonst77/LexRadar.git
cd LexRadar
git checkout feat/release-batch-readiness
git rev-parse HEAD  # Record and compare with reviewed PR #17 SHA
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m playwright install --with-deps chromium
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest -q
```

System dependency installation needs that machine owner's normal package-install
permission. If unavailable, ask its administrator to install Playwright dependencies.
Do not improvise a bypass of restrictions.

Offline browser/proxy check (no HTTP/DNS request):

```bash
.venv/bin/python - <<'PY'
import os
from playwright.sync_api import sync_playwright
names = ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy')
if any(os.environ.get(k) for k in names):
    raise SystemExit('STOP: mandatory/configured proxy; existing Collector incompatible. Do not unset.')
with sync_playwright() as p:
    browser = p.chromium.launch(timeout=15000)
    context = browser.new_context(java_script_enabled=False, service_workers='block')
    page = context.new_page()
    page.set_content('<p>Offline browser check</p>')
    assert page.inner_text('body') == 'Offline browser check'
    browser.close()
print('Offline browser OK; public egress/robots still need authorized smoke.')
PY
```

## Authorized small smoke (only after above checks)

The following command WILL request the URLs supplied by the user. Do not run it
in the current restricted development environment.

```bash
.venv/bin/lexradar batch-screen /private/path/smoke-urls.txt \
  --output /private/path/smoke-run-001 --workers 1 --max-sites 5 \
  --max-pages 3 --max-documents 2 --max-requests 16 \
  --crawl-duration 60 --site-timeout 180 --min-delay 2
# Resume only with identical URLs, options, code/dependencies and normative inputs:
.venv/bin/lexradar batch-screen /private/path/smoke-urls.txt \
  --output /private/path/smoke-run-001 --workers 1 --max-sites 5 \
  --max-pages 3 --max-documents 2 --max-requests 16 \
  --crawl-duration 60 --site-timeout 180 --min-delay 2 --resume
```

Inspect review-queue.html, batch.json, each attempt's logs and dossier/collection.json,
crawl/requests.jsonl and crawl/summary.json. Confirm at least one actual available
page with original HTML/hash/time/status, respected robots/pacing, and independently
reproduced observations. HTTP errors, DNS/proxy/browser failures and incomplete
coverage must remain explicit. Check process/memory/disk usage before increasing
workers. No submitted forms, JS, authorization, CAPTCHA bypass or LLM request.
A successful offline browser check does not confirm network availability.

P0 still open: actual authorized machine/access, Chromium+direct egress acceptance,
small real smoke and resource observation. Current development cannot certify these.

## Updating after the robots compatibility fix

The four initial operator runs on 12 October stopped before content: doktstom.ru
returned robots 404, vita-centr40.ru returned 403, scz.ru redirected, and www.scz.ru
returned a policy containing leading-wildcard records. These are crawl diagnostics,
not legal findings. Chromium was separately verified offline on the operator server.

After checking out the reviewed fix commit, reinstall with
`.venv/bin/python -m pip install -e .`. Preserve old run directories. Use a new output
directory after code changes; `--resume` rejects a different engine fingerprint.
Start with the canonical `https://www.scz.ru/` (cross-origin redirects, including
www changes, remain blocked). A 403 remains blocked; do not retry it under a disguised
user agent. 404/410 now record absent robots, while 200 policies retain applicable
Disallow and Crawl-delay. Same-origin redirects use the existing network budgets.
The operator must still inspect collection.json and original page artifacts before
calling the real collection successful.

Protocol reference: https://www.rfc-editor.org/rfc/rfc9309.html (sections 2.2, 2.3.1).
The 403/429 and ambiguous selected-group policy is intentionally stricter.
