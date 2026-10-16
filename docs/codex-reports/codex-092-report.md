# Контрольная точка LexRadar №92 — 2026-10-16

Назначение: сохранение кода после №91, без deploy, merge, rebase и AI-запросов.

Исходная ветка: feat/compact-ai-audit-mvp.
Исходный HEAD: de538e76417892db3396b06c7ecfa166b9f02eac.
main = origin/main = 8104f17960d17f13e33e332eaa413cd0b0012228; main не изменяется.
Origin: https://github.com/golkonst77/LexRadar. Fetch успешно выполнен 2026-10-16.
Коммитов локальных веток вне origin нет. Отдельно существует локальный stash
ccc08c7 (с родителями d6e5760, be41150); он не включается и не изменяется.

## Классификация

A: все изменённые и новые src/lexradar модули и промпты, .gitignore.
Включён experiment_069: он импортируется производственным orchestrator.
B: изменённые и новые tests, включая синтетические тесты №84–85 и исторический
снимок транспорта; приватные фикстуры не копируются.
C: изменённые инструкции, batch runbook, синтетические CSV, технические отчёты
№84 и №91, неизменённый утверждённый шаблон и этот отчёт.
D: старые незакоммиченные отчёты, scripts/build_review_069.py,
платные pilot-скрипты, proposal-080-config.json и прочие экспериментальные
материалы исключены; некоторые исторические тесты зависят от этих скриптов.
E: все runs-*/ (пакеты клиентов, ответы, approvals, логи), локальные секреты,
.env, .aws/.codex, кеши и случайный файл в корне не добавляются.
Существующие файлы не удаляются и не исправляются.
Публичные реквизиты утверждённого шаблона уже опубликованы в его исходной
GitHub-ветке; оригинал сохранён побайтно. Имена в исходном коде и пути к
локальным досье не сопровождаются содержимым клиентских досье.

## Связь с заданиями №84–91

84: client_proposal, proposal_export, утверждённый КП, тест и технический отчёт.
85: mvp_batch и синтетический tests/test_mvp_batch_085.py.
86–88: локальные сборы/пакеты/проверки и одобрения в runs-086–088; не публикуются.
89–90: локальные модельные эксперименты в runs-089–090; не публикуются.
91: GPT-5 Mini, профиль и переключение модели, cookie schema/prompt/validator,
обзор, запрет Single A retries — сохранены в src и техническом отчёте.
Точная атрибуция каждой строки более ранним заданиям не доказуема:
промежуточных коммитов нет; атрибуция основана на локальных отчётах и тестах.

## Шаблон

Путь: templates/approved/КП LexRadar__v2_4.html.
SHA-256: 7e352c2597ff272bcf25095ac442411da2fc291bdbb0559cf0dd116970de7b5e.
Побайтное совпадение с origin/review/approved-kp-v2-4 подтверждено.
Коммерческий текст не изменялся.

## Воспроизведение

README.md, pyproject.toml, примеры и инструкции доступны в дереве коммита.
Python >=3.12; python -m venv .venv; .venv/bin/python -m pip install -e '.[dev]'.
Для браузерных сценариев отдельно требуется Chromium; для PDF — доступный шрифт.
Платный режим требует внешнего ключа и разрешения на передачу данных;
секреты и приватные данные намеренно не являются частью репозитория.

Проверка в исходном дереве: 46 выбранных тестов, 36 passed / 10 failed.
Падения: test_mvp_batch_085 (1), test_diagnostics_060 (5),
test_transport_061 (1), test_historical_openrouter_061 (3).
Причины: старые ожидания DeepSeek/timeout/retries и ERROR в batch №85 после №91.
Все 14 тестовых случаев runs-091/test_task091.py в этом запуске прошли;
сам файл зависит от приватных runs-088–090 и не публикуется.
Полный набор тестов не запускался. Это снимок состояния с известными регрессиями,
а не подтверждение готовности pre-production.

Проверка восстановления выполняется отдельно из git archive в пустом /tmp:
сравнение всех src, тестов, шаблона и метаданных сборки с исходным деревом,
компиляция и выбранные офлайн-тесты без доступа к незакоммиченным файлам.
Итоговый SHA, remote SHA и фактический результат записываются в локальный
runs-092/checkpoint-report.md после push. Полные исходные status и branches
сохранены локально в runs-092, включая список несохранённых файлов.

## Явный список добавляемых файлов

- `.gitignore`
- `docs/codex-reports/codex-084-report.md`
- `docs/codex-reports/codex-091-report.md`
- `docs/examples/mvp-066-leads.csv`
- `docs/examples/mvp-066-one-local.csv`
- `docs/mvp-batch-runbook.md`
- `docs/mvp-cli.md`
- `docs/single-auditor-050.md`
- `src/lexradar/audit_capture.py`
- `src/lexradar/audit_documents.py`
- `src/lexradar/audit_review.py`
- `src/lexradar/auditors/compact_pipeline.py`
- `src/lexradar/auditors/grounding.py`
- `src/lexradar/auditors/models.py`
- `src/lexradar/auditors/orchestrator.py`
- `src/lexradar/auditors/prompts/single-a-v1-schema.txt`
- `src/lexradar/auditors/prompts/single-a-v2.txt`
- `src/lexradar/auditors/prompts/single-a-v3-cookie.txt`
- `src/lexradar/auditors/provider.py`
- `src/lexradar/auditors/reconciliation.py`
- `src/lexradar/auditors/validator.py`
- `src/lexradar/batch_paid_guard.py`
- `src/lexradar/batch_qualification.py`
- `src/lexradar/client_proposal.py`
- `src/lexradar/commercial_preview.py`
- `src/lexradar/document_review.py`
- `src/lexradar/evidence_review.py`
- `src/lexradar/experiment_069.py`
- `src/lexradar/mvp.py`
- `src/lexradar/mvp_batch.py`
- `src/lexradar/mvp_demo.py`
- `src/lexradar/proposal_export.py`
- `src/lexradar/review_bundle.py`
- `src/lexradar/saved_audit.py`
- `src/lexradar/single_auditor.py`
- `src/lexradar/single_auditor_live.py`
- `templates/approved/КП LexRadar__v2_4.html`
- `tests/fixtures/openrouter_061/historical-evidence.json`
- `tests/fixtures/openrouter_061/transport_ccc08c7.py.txt`
- `tests/test_approved_proposal_084.py`
- `tests/test_audit_review_067.py`
- `tests/test_auditors.py`
- `tests/test_client_proposal_080.py`
- `tests/test_compact_paid.py`
- `tests/test_compatibility_077.py`
- `tests/test_diagnostics_060.py`
- `tests/test_experiment_069.py`
- `tests/test_grounding_076.py`
- `tests/test_historical_openrouter_061.py`
- `tests/test_integration_066.py`
- `tests/test_mvp.py`
- `tests/test_mvp_batch_063.py`
- `tests/test_mvp_batch_085.py`
- `tests/test_paid_pilot_039.py`
- `tests/test_pilot_042.py`
- `tests/test_review_bundle.py`
- `tests/test_streamlake_071.py`
- `tests/test_transport_061.py`
