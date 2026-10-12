# Readiness review and minimal two-pass release — 12 October 2026

Reviewed main: `047deedc58e7d9e021c9304d4ce38de2037bb968`.
Target: 15 October 2026, Moscow time. No deployment or merge in this change.

## Actual path and release gaps

| Step / code | Works today | Evidence / limitation | Priority |
|---|---|---|---|
| URL list → batch | Added `batch.run_batch`, `batch-screen` | Synthetic queue/concurrency/restart tests; no real 1000-site run | P1 |
| Pages/PDF → evidence: `collector.runner.collect`, `network.Fetcher`, `crawl.CrawlSession` | DNS-pinned GET, SSRF/redirect checks, robots, delays, byte/page/request/deadline limits, hashes, isolated screenshots | Network/browser tests synthetic; real saved HTML was supplied manually. Live production collector not validated in current environment | P0 environment and authorized smoke |
| Static analysis: `primary.service.audit_saved_dossier` | 13 bounded stages, forms, integrations, roles, partial spans, internal hypotheses | Real offline HTML + synthetic tests; no active JS/form submission or proof of real processing | P1 real collection validation |
| Independent reproduction: `verifier.offline.verify_saved_assessment` | Reloads saved bytes, compares facts and citations | Local reproducibility, not an independent legal expert or fresh full-site research | Working bounded path |
| Normative comparison: `requirements.verify_normative` | Source integrity, exact quotations, conditions, gaps, research context | Every production source remains untrusted. Research PD01/PD09/PD06 checked with user-attested/SPS scope only; medicine unverified | P1 scoped second-pass review |
| Internal reports: `primary.cli.write_report`, `requirements.export_normative` | JSON matrices with limits and HOLD; batch JSON + HTML queue | No client letter sent; files are internal, not a complete legal opinion | Working / P1 usability |
| Human review: `decision.service.decide_production`, `ReviewBinding` | Exact bindings and untrusted human submissions; cannot forge GO | No authenticated reviewer or trusted legal provenance provider; queue is navigation, not approval UI | P1 manual recorded procedure; production client release remains unavailable |
| Export/send: `approval.send_client_message` | Internal exports work; production send always blocked | No authenticated client-release interface; no sending implementation | Outside first screening release; never weaken to meet date |

HOLD is a client/legal gate, not a job failure. Batch proceeds to the next site.

## Authorized execution environment — P0

Current managed development instance enforces a restricted proxy policy, excludes site
hosts and lacks Chromium. In addition, `Fetcher.get` explicitly refuses mandatory
proxy environments: its DNS-pinned transport requires direct egress. Merely adding
an HTTP allowlist or installing a browser does NOT make this collector usable.
Do not unset proxy settings or bypass the policy in that instance.

Use a separately authorized Linux/Python 3.12 execution environment with permitted
public DNS resolution and direct HTTP/HTTPS egress, trusted CA validation, and
Playwright Chromium/system dependencies installed. POSIX process groups and `flock`
are required by the initial batch implementation. No server application, CRM,
Docker deployment or proxy rewrite is needed. Files must be kept in a private
writable local directory outside public commits. A single batch directory has an
exclusive process lock. Do not run parallel batches on the same sites: per-host
scheduling applies within one batch only. Host aliases may identify the same site;
check the input list before a production run.

Check browser executable presence and browser launch offline. In the authorized
environment run the synthetic CI suite, then 3–5 explicitly allowed real sites
with a readable robots file. Include slow/error/redirect/document cases in an
owned synthetic server suite. Record environment, SHA, receipt times, URL/status,
robots journal, requests, original HTML/PDF hashes, coverage and errors. Review
partial screenshot failure and retained HTML. Network refusal is technical_retry,
not a missing policy. `www`/non-`www` cross-origin redirects may be refused by the
existing policy; record this conservatively, never weaken SSRF to increase yield.

## First small change in this PR

`lexradar batch-screen` accepts up to 1000 unique canonical URLs. Default 1 worker,
maximum 4, one active job per hostname (including different schemes). Each site
runs in an isolated process group, with a 180-second total deadline including
analysis and children. The parent retains logs/bytes on failure and continues.
3 HTML pages, 2 PDFs, 16 request attempts and 60 crawl seconds per site; at least
2 seconds between requests, raised by robots Crawl-delay. Existing byte/PDF
limits unchanged (5 MB/file, 50 MB collected/site). Snapshot verification has a
250 MB upper bound per attempt. Provision disk for up to 250 GB per 1000-site
attempt round; check free space before starting and monitor it. Collector partial
files may exist without a completed collection.json after worker termination;
they are retained as interrupted artifacts, not represented as a complete dossier.

Atomic fsynced progress records, canonical duplicate elimination, same-plan resume,
file hashes on reuse, fresh attempt directories on retries, logs and HTML/JSON
manual review queue. Resume skips successful sites, including HOLD/low_priority;
technical_retry repeats in a NEW attempt without erasing earlier evidence.
The process lock is advisory against other LexRadar batch processes, not against
an adversarial local writer. Batch JSON is untrusted and never a legal approval.

No LLM calls. LLM budget/spend is $0. The pipeline reuses primary-audit,
verify-primary and optional verify-norms. No confidence/model agreement yields GO.

### Initial ranking (superseded by follow-up below)

- priority_review: a fully read saved policy title explicitly names a different
  domain from its source. Cited evidence/bounds retained; affiliation/multisite
  explanation still needs review. No automatic violation.
- waiting: form or policy found but no supported strong mismatch; more evidence needed.
- low_priority: no supported strong signal in observed scope; never 'legally clean'.
- technical_retry: invalid URL, no usable collection, worker timeout/crash or stage failure.

Two separate heuristic scores: static evidence support and conditional fit for
legal documents/webmaster specification. Neither establishes violation or paid
necessity. First-pass recall is NOT validated; narrow matching can miss many
promising sites. Do not rank GTM, external links, no checkbox, multiple mentions
or unobserved documents as strong signs. No quota or minimum candidate count.

## Plan 12–14 October

| Date | P0/P1 action | Measurable acceptance |
|---|---|---|
| 12 Oct | Queue/worker/restart/ranking PR and readiness review (this PR) | Synthetic malformed URL, timeout descendant kill, failed site continuation, dedup, same-host serial, hash change, interrupted resume; full CI; 1000-input smoke |
| 13 Oct | Authorized environment + real limited first-pass smoke; inspect yields and missing evidence | 3–5 authorized real sites; no forms/JS/LLMs; URLs/times/hash manifests/robots journal retained; failed site never labelled clean; disk capacity confirmed |
| 13 Oct | Validate ranking on 10–20 labelled local saved cases, including distractors | Every priority row has exact positive evidence and reason; zero priorities from checkbox/GTM/link alone; partial/unread docs do not become absence; manual review false positives recorded |
| 14 Oct | Second pass on selected actual candidates, complete public-document reading + human worksheet | For each: evidence IDs, whole/partial page inventory, exact norm/date/scope/person/exceptions, contrary evidence, sufficiency result, correction/new document/technical-only/not-needed/unknown; no paid necessity before existing-doc review |
| 14 Oct | 1000-URL permitted batch rehearsal + recovery exercise, freeze tested SHA | All inputs accounted for; duplicate count; completed/retry counts; stop/resume preserves prior files; bounded worker/request/time and $0 LLM budget; manual queue opens locally; final CI green |

1000-site direct run is allowed only after environment and small real smoke acceptance,
not because the synthetic scheduler accepts 1000 inputs. Worst-case 1000 distinct
hosts at 180 seconds and 4 workers is ~12.5 hours plus orchestration; repeated same
hostname can take longer. With 1 worker it is ~50 hours. Measure real throughput
on 13 October before promising 15 October completion.

P2: OCR, registry automation, broad semantic screening, paid A/B, automatic
commercial scoring, authentication/service deployment. No mass communication.

## Second pass and normative release scope

Select priority_review rows manually; waiting can also be selected with a reason.
Use a NEW fuller collection directory, within site limits, and existing commands
`primary-audit`, `verify-primary`, `verify-norms`. Read all related documents and
PDF pages. Scan/unread page means additional visual review, not a violation.

Start only with research PD01 (specified art.6 alternatives/necessity), PD09
(art.9(1) separate consent only when used), PD06 (art.18.1(2) policy and protection
information). Registry needs exact saved sources and explicit normative date.
Do not pass the 44-card research JSON directly as a SourceRegistry: schemas and
trust semantics differ. Do not silently apply the 11 October normative checkpoint
to a 15 October website observation; check intervening changes or state uncertainty.
Medicine, advertising and liability remain research questions until their exact
provisions/currentness and applicability are sufficiently reviewed.

A human worksheet should record independent evidence strength; conditional
commercial fit; operator; purpose and alternative basis; norm revision; exceptions;
all relevant documents/pages read; contradiction or unresolved question; correction
of existing document / new document / technical change / no work / unknown;
service fit (documents + webmaster specification); reviewer/date; dossier/finding/
norm/client-text hashes when applicable. This PR does not implement trust or client
approval. Even a recorded human conclusion remains untrusted to production Gateway.

## Launch boundary

An internal two-pass screening/review tool can be released after the above gates.
An automated legally confirmed client-release system is NOT ready and is not
promised for 15 October. No passing synthetic tests substitutes for real collector
acceptance or human normative/applicability review. No client export/send in this PR.

## Checks performed in development

26 new synthetic regressions pass, including a descendant that starts a separate
process group, CLI analysis of a saved synthetic dossier, same-host concurrency,
exclusive lock, changed engine/source fingerprints and orphan-worker recovery.
Resume identifies an unfinished worker using PID start time and exact worker command
before stopping it; an editable PID alone cannot kill an unrelated process. A 1000-unique-URL
scheduler/files test completed all jobs in 33.12 seconds; same-plan resume in
0.31 seconds performed no additional jobs. No network/browser/LLM was used in
that load test; it does not establish real crawl throughput or screening recall.
Ruff and formatting pass. Full browser-dependent suite is checked in GitHub CI;
Chromium cannot be installed/used for that suite in the current restricted worker.

## PR #17 follow-up review — 12 October

Fixed: missing/invalid normative registry no longer blocks batch startup;
normative-stage failure no longer discards successful collection/analysis;
it remains a recorded normative gap. An existing interrupted attempt directory
is preserved and a fresh number allocated. Missing saved attempt directories
invalidate reuse rather than silently passing an empty snapshot comparison.

Screening now also checks positive, bounded contradictions: explicit operator and
executor INN difference (role clarification, not a violation), full policy denying
phone collection versus a fully inspected telephone form, and a recorded HTML/PDF HTTP
404/410 for a policy link in that complete form. HTTP metadata is bound to the saved
manifest hash. Timeouts, partial docs/forms, biographies and multiple organization
mentions cannot independently create these priorities. Known explicit multi-site
scope and processing-on-behalf explanations suppress the relevant mismatch.
All normative statuses remain research_question pending applicability/currentness.

Required-information absence remains deferred: Collector does not establish the
website denominator, actual obligated person or applicable obligation. No rule
pretends that three collected pages establish sufficient absence coverage.

18 saved synthetic cases are parsed by the existing primary/independent readers.
The expected review labels and known limitations are retained in tests/test_screening.py.
16 controlled cases match their labels. Two challenge cases expose a known miss
(paraphrased phone denial) and a false priority (related domains explained without
recognized scope wording). These are not real-site precision/recall estimates.
The source wording/role uncertainty goes to second pass; no violation/paid need
is asserted. Automatic coverage of PDFs/scanned policies and semantic contradictions
remains limited. Do not select a quota or relabel cases to hide these gaps.

Use [today's installation/smoke checklist](authorized-run-environment.md).

Cross-job pacing is also enforced: consecutive jobs/retries use the recorded maximum
Crawl-delay (at least min-delay), including resume; www/non-www share a load slot.
Other aliases remain an input-review limitation. A pacing wait longer than the site
budget is deferred as technical_retry without sending a request, not silently ignored.
