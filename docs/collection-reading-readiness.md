# Reading readiness after the first real run

**Consolidation scope:** the scz.ru measurements below are historical observations
recorded in PR #19. They were not reproduced during branch consolidation and do
not establish that the real scz.ru audit or acceptance run has passed.

The scz.ru smoke established usable collection, but policy PDF (5,400,863 bytes)
exceeded the default 5 MB bound; increasing it to 20 MB obtained a 12-page scan.
The saved pages also contained SVG self-closing shape tags. The strict extractor
previously quarantined the HTML suffix, losing the subscription section. This
patch accepts inert SVG shapes without accepting malformed HTML script delimiters.
On saved page-0001, extraction increased from 5,817 to 12,069 characters and
includes the subscription block. This is static extraction, not live execution
or proof of complete site coverage. Old dossiers remain immutable and should
not be resumed across engine changes.

## One normal command, no Python workaround

```bash
.venv/bin/lexradar batch-screen work/smoke-urls-004.txt \
  --output work/smoke-run-007 --workers 1 --max-sites 1 \
  --max-pages 3 --max-documents 4 --max-file-bytes 20000000 \
  --max-total-bytes 50000000 --max-requests 16 \
  --crawl-duration 120 --site-timeout 300 --min-delay 2
```

Both `collect` and `batch-screen` expose file/total-byte bounds (maximum 20/200 MB).
Defaults remain unchanged. Batch propagates options to the collector and binds
these values in its resume fingerprint. Legal policies, consents and recipient
lists are preferred over unrelated PDFs. Document link text is used alongside
the decoded URL; ties remain deterministic. Not all discovered documents fit a
bounded run, and selection is not an absence finding.

The queue JSON now lists `reading_gaps` with evidence IDs, URLs and extraction
states. Incomplete text cannot silently become a low-priority clean-site result.
The HTML queue shows the count of gaps in reasons. HOLD and client/send prohibitions
remain unchanged.

## Offline OCR assistance for already saved scans

Install system dependencies once on the Linux execution server:

```bash
sudo apt-get install -y poppler-utils tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng
```

Use the already saved dossier; no new request to the website and no paid API:

```bash
.venv/bin/lexradar ocr-document work/scz-expanded-001 \
  --evidence document-0001 --output work/scz-policy-ocr-001 \
  --language rus+eng --max-pages 30 --timeout 240
```

Output: `ocr.json`, verified immutable copy `original.pdf`, and numbered page PNGs
and UTF-8 TXT files. Source PDF/manifest hashes and page image/text hashes bind the
results to the original evidence. Page/time/file/total output bounds apply; partial
results survive rendering/extraction errors. Missing dependencies/languages stop
before claiming OCR success. Input symlinks/hash mismatches and outputs inside the
source dossier are rejected.

**OCR is assistance, not verified visual reading or a legal decision.** The sidecar
is not automatically imported into primary-audit, verify-primary or ranking. Check
citations against page images and use the text for controlled expert review. Existing
reading metadata remains unchanged; the scan continues to require review. Integration
of verified OCR citations into the automated research pipeline remains separate work.

## Verification

Regression tests cover SVG suffix preservation, malformed scripts, preference of
legal PDFs, visible unread-document gaps, byte limits, original integrity and a
real offline OCR operation with page/source hashes. CI installs OCR languages and
Chromium and runs the full existing suite. No claim of improved legal precision or
recall is made from these technical fixes.
Batch supervision and offline OCR execution require Linux; Windows supports
common Collector/reading and screening code and reports unsupported batch/OCR
execution explicitly.
