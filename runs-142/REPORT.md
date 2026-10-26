# LexRadar №142 — result

## Changes

- Content redirects may cross origins only through the existing bounded fetcher. Before requesting the redirected page, the collector fetches and applies that origin's `robots.txt`. DNS/public-address validation, request and time budgets, pacing, and redirect limits remain active. Robots redirects themselves remain same-origin only.
- Robots parsing errors and non-permission HTTP responses remain deny-all. The collector retains the concrete reason, and prepare mode records a site with no collected evidence as `SKIPPED` instead of a batch `ERROR`. A missing redirect location is not retried or bypassed; it is recorded as a per-site skip reason.
- Robots response artifacts for distinct origins are kept separately so a redirect target cannot overwrite the initial site's artifact.
- The batch CLI now prints selected, successful, skipped with reason counts, errors, signals, draft count, elapsed time, and report paths. The existing `--max-sites` limit of 10 was retained; resume now counts previously attempted skips toward that limit.

The approved proposal template, prices, discount, HTML/PDF rendering, and manual approval flow were not changed. No CSV parser or source CSV was changed.

The branch includes the existing MVP batch source and its required runtime modules so the CLI changes can be reviewed in context. Prior audit reports, raw provider fixtures, client CSVs, and run diagnostics are excluded.

## №140 cases

The published №140 summary recorded 15 sites: 1 draft, 5 HOLD, and 9 errors. This change addresses how the listed collection failures are handled:

- Invalid `robots.txt` user-agent syntax remains blocked and is now a precise skip reason.
- Robots HTTP 403, 425, and 444 remain blocked; none grants crawl permission.
- A missing or exhausted redirect remains blocked and is reported per site.
- A cross-origin content redirect is followed only after the target origin's robots rules allow it.
- Explicit `robots_disallow` remains enforced; no access restriction is bypassed.

These changes do not remove external access restrictions. Sites that deny or do not clearly permit collection remain skipped with the recorded reason.

## Checks

- PASS — mocked-transport checks for allowed cross-origin redirect ordering, target-origin `robots_disallow`, target robots HTTP 403, initial robots HTTP 403/425/444, invalid robots agent, and missing `Location`.
- PASS — local synthetic batch check: zero-evidence prepare row becomes `SKIPPED` with its exact robots reason; the 1-site cap remains in force after resume.
- PASS — CLI summary and `--max-sites 10` argument parsing; existing input deduplication remains in place.
- PASS — Python compilation and `git diff --check` for changed modules and tests.
- Not run — pytest is not installed in the available Python environments. The socket-based fixture could not bind localhost in the sandbox; the collector checks above used a mocked HTTP transport and made no network requests.
- No live-site collection, mass run, or paid AI request was made.

## Next approved batch command

```bash
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python -m lexradar.mvp_batch INPUT.csv --output runs-143 --mode prepare --max-sites 10 --allow-site-collection
```

This command collects evidence only; it does not run an AI audit or send a proposal.
