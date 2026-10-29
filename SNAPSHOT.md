# Independent source snapshot — 2026-10-29

This repository starts with one new root commit. It contains current LexRadar
Python sources, CLI entry points, scripts, offline fixtures/tests, selected
technical documentation, and the mailer sources under `mailer/`.
Sources were copied without following symbolic links; no symlinks were found
in the selected source directories. Original worktrees and mailer deployment
were read only. No mail or live CRM operation was performed. Parser v3 was
outside the scope and was not accessed.

## Publication changes

Real CRM sample IDs, sender address and internal API URL were replaced with
fictional examples consistently in espo-export, its tests, and mailer.
Mailer deployment config was replaced by disabled/dry-run `*.example.json`
files; its run instructions were rewritten for the snapshot. Historical
provider request identifiers in one pilot script became fictional examples.
The commercial sales domain became `sales.example.invalid`; two real-company
TINs used in a synthetic test became sequential dummy numbers.

## Excluded material requiring review before restoration

- `approved-templates/КП LexRadar__v2_5.html` and
  `templates/approved/КП LexRadar__v2_4.html`: real contact information, including
  in UTF-16 HTML. Not certified anonymous. Their bytes were not edited to bypass
  template integrity checks. Proposal rendering and related tests require a
  separately approved safe template and matching integrity constants.
- `scripts/proposal-080-config.json`: real client configuration.
- `docs/codex-reports/`, real-run acceptance/runbook/history documents,
  dated collection/health replay documents, and documentation CSV inputs:
  unnecessary client/run evidence excluded. The deployment-specific Espo guide
  was replaced with safe run instructions.
- mailer `VERIFICATION.md`, config, batches, approvals, backups and journal:
  deployment/client state excluded; examples contain no approvals.
- historical `examples/compact-first-pilot/openrouter-metadata*.json`:
  operational provider metadata excluded.

All `runs*`, `work/`, original Git metadata, caches, databases, credentials,
.env files, attachments and PDFs were excluded. Existing example dossiers and
reports retained here use synthetic clinic/example domains and fixture data;
no real-client reports were intentionally retained.

## Local validation

- Mailer: 22 unittest tests passed, fake API only.
- Espo export: 17 unittest tests passed, fake API only.
- Every included Python file passed AST syntax parsing.
- Broader LexRadar pytest checks could not run: pytest is not installed.
  No packages were downloaded. Full application/dependency and PDF checks
  were not performed; excluded approved templates also limit proposal tests.
- Content checks covered credential patterns, email addresses, phones,
  CRM-style IDs, URLs, TIN/company/person markers, and long encoded literals.
  UTF-16 templates were decoded before exclusion. Remaining email addresses
  use reserved example domains; phone/person examples are synthetic test data.
  Remaining 17-digit numeric matches are decimal metric fragments, not CRM IDs.
  Binary fixtures are three identical 68-byte 1x1 PNGs, matching the test pixel.
  Pattern scanning is evidence for this review, not a universal anonymity proof.
