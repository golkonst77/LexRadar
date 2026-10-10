"""Sequential bounded crawl. Browser renders fetched bytes with all networking disabled."""

import os
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Error as BrowserError
from playwright.sync_api import sync_playwright

from .artifacts import ArtifactStore, verify_integrity
from .crawl import CrawlOptions, CrawlSession
from .extract import DOCUMENT_EXTENSIONS, DOM_SCRIPT, categories, entities
from .models import (
    CollectedEvidence,
    CollectionResult,
    DocumentObservation,
    FormObservation,
    Limits,
    PageObservation,
)
from .network import CollectionError, Fetcher, canonical_url, origin
from .pdf import extract_inventory, legacy_result
from .reading import extract_html


def now():
    return datetime.now(UTC)


def finalize_evidence(evidence: CollectedEvidence) -> None:
    if evidence.artifacts:
        evidence.available = True
        evidence.unavailable_reason = None
        evidence.status = "partial" if evidence.collection_errors else "complete"
        evidence.observed_fact = (
            "Partially collected artifacts retained; see collection_errors; no legal conclusion"
            if evidence.status == "partial"
            else "Artifacts captured with individual representations; no legal conclusion"
        )
    else:
        evidence.status = "unavailable"
        evidence.unavailable_reason = (
            "; ".join(evidence.collection_errors) or "No artifacts captured"
        )
        evidence.available = False
        evidence.observed_fact = "No artifacts captured; reason recorded without assumptions"


def collect(
    url: str,
    output: Path,
    limits: Limits | None = None,
    *,
    fetcher: Fetcher | None = None,
    crawl_options: CrawlOptions | None = None,
) -> CollectionResult:
    limits = limits or Limits()
    url = canonical_url(url)
    allowed = origin(url)
    store = ArtifactStore(output, limits)
    session = CrawlSession(fetcher or Fetcher(limits), url, store.root, crawl_options)
    try:
        return _collect(url, limits, allowed, store, session)
    except BaseException:
        session.finish("collector_error")
        raise


def _collect(url, limits, allowed, store, session):
    result = CollectionResult(target_url=url, started_at=now(), limits=limits)
    queue = deque([url])
    seen = {url}
    document_urls = set()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ.get("LEXRADAR_CHROMIUM_PATH"),
            timeout=min(30, session.remaining()) * 1000,
        )
        context = browser.new_context(
            java_script_enabled=False,
            service_workers="block",
            accept_downloads=False,
            viewport={"width": 1280, "height": 900},
        )
        try:
            while queue and len(result.pages) < limits.max_pages:
                try:
                    session.check()
                except CollectionError:
                    break
                current = queue.popleft()
                eid = f"page-{len(result.pages) + 1:04d}"
                timestamp = now()
                evidence = CollectedEvidence(
                    id=eid,
                    source=current,
                    captured_at=timestamp,
                    observed_fact="Page could not be checked",
                    available=False,
                    unavailable_reason="Not collected",
                )
                page_record = PageObservation(
                    requested_url=current, captured_at=timestamp, evidence_id=eid
                )
                result.evidence.append(evidence)
                result.pages.append(page_record)
                page = None
                try:
                    response = session.get(current, allowed)
                    evidence.source = response.url
                    evidence.captured_at = now()
                    page_record.captured_at = evidence.captured_at
                    page_record.final_url = response.url
                    page_record.http_status = response.status
                    if not 200 <= response.status < 300:
                        raise CollectionError(f"HTTP {response.status}")
                    if "text/html" not in response.content_type.lower():
                        raise CollectionError("Page is not HTML")
                    try:
                        evidence.artifacts.append(store.save(f"{eid}.html", response.body, "html"))
                    except (CollectionError, OSError) as exc:
                        evidence.collection_errors.append(f"html: {str(exc)[:300]}")
                    page = context.new_page()
                    page.set_default_timeout(
                        min(limits.timeout_seconds, session.remaining()) * 1000
                    )
                    # Only this one main navigation is fulfilled; every subrequest is aborted.
                    served = False

                    def route_request(route, request, response=response):
                        nonlocal served
                        if (
                            not served
                            and route.request.is_navigation_request()
                            and (route.request.url == response.url)
                        ):
                            served = True
                            route.fulfill(
                                status=200, content_type=response.content_type, body=response.body
                            )
                        else:
                            route.abort()

                    page.route("**/*", route_request)
                    page.goto(response.url, wait_until="domcontentloaded")
                    page_record.title = page.title()
                    dom = page.evaluate(DOM_SCRIPT)
                    page_record.text, page_record.reading = extract_html(
                        response.body, min(limits.max_file_bytes // 4, 1_000_000)
                    )
                    original = next((a for a in evidence.artifacts if a.kind == "html"), None)
                    page_record.reading.original_saved = original is not None
                    page_record.reading.source_sha256 = original.sha256 if original else None
                    page_record.reading.provenance = (
                        "reproduced" if original else "missing_original"
                    )
                    page_record.reading.text_coverage_complete = (
                        original is not None
                        and page_record.reading.extraction_state == "complete"
                        and not page_record.reading.text_truncated
                        and bool((page_record.text or "").strip())
                    )
                    page_record.categories = categories(current + " " + page_record.title)
                    for stage, capture in (
                        (
                            "text",
                            lambda eid=eid, record=page_record: store.save(
                                f"{eid}.txt", record.text.encode(), "text"
                            ),
                        ),
                        (
                            "screenshot",
                            lambda eid=eid, page=page: store.save(
                                f"{eid}.png", page.screenshot(), "screenshot"
                            ),
                        ),
                    ):
                        try:
                            page.set_default_timeout(
                                min(limits.timeout_seconds, session.remaining()) * 1000
                            )
                            evidence.artifacts.append(capture())
                        except (CollectionError, BrowserError, OSError) as exc:
                            evidence.collection_errors.append(f"{stage}: {str(exc)[:300]}")
                    result.entities.extend(entities(page_record.text, response.url))
                    for index, form in enumerate(dom["forms"]):
                        if session.stop_reason:
                            break
                        page.set_default_timeout(
                            min(limits.timeout_seconds, session.remaining()) * 1000
                        )
                        links = form.pop("links")
                        observation = FormObservation(
                            page_url=response.url,
                            **form,
                            policy_links=[
                                x["url"]
                                for x in links
                                if "privacy_policy" in categories(x["text"] + x["url"])
                            ],
                            consent_links=[
                                x["url"]
                                for x in links
                                if "consent" in categories(x["text"] + x["url"])
                            ],
                        )
                        try:
                            observation.screenshot = store.save(
                                f"{eid}-form-{index + 1}.png",
                                page.locator("form").nth(index).screenshot(),
                                "form_screenshot",
                            )
                            evidence.artifacts.append(observation.screenshot)
                        except (CollectionError, BrowserError, OSError) as exc:
                            evidence.collection_errors.append(f"form_screenshot: {str(exc)[:300]}")
                            observation.unavailable_reason = str(exc)[:300]
                        page_record.forms.append(observation)
                    links = sorted(
                        dom["links"],
                        key=lambda x: (not bool(categories(x["text"] + x["url"])), x["url"]),
                    )
                    for link in links[: limits.max_links_per_page]:
                        try:
                            target = canonical_url(link["url"])
                            if origin(target) != allowed:
                                continue
                            is_document = (
                                urlsplit(target).path.lower().endswith(DOCUMENT_EXTENSIONS)
                            )
                            if is_document:
                                page_record.document_links.append(target)
                                document_urls.add(target)
                            elif target not in seen and len(seen) < limits.max_pages * 10:
                                seen.add(target)
                                queue.append(target)
                        except CollectionError:
                            continue
                    page_record.document_links = sorted(set(page_record.document_links))
                except (CollectionError, BrowserError, OSError) as exc:
                    evidence.collection_errors.append(str(exc)[:500])
                finally:
                    if page:
                        try:
                            page.close()
                        except BrowserError as exc:
                            evidence.collection_errors.append(f"page_close: {str(exc)[:300]}")
                    finalize_evidence(evidence)
                    if evidence.artifacts:
                        evidence.artifact_path = evidence.artifacts[0].path
                        evidence.sha256 = evidence.artifacts[0].sha256
            if queue:
                result.warnings.append("Page crawl limit reached")
        finally:
            for close in (context.close, browser.close):
                try:
                    close()
                except BrowserError as exc:
                    result.warnings.append(f"Browser cleanup failed: {str(exc)[:300]}")
    pdf_urls = [u for u in sorted(document_urls) if urlsplit(u).path.lower().endswith(".pdf")]
    if len(pdf_urls) > limits.max_documents:
        result.warnings.append("PDF download limit reached")
    for current in pdf_urls[: limits.max_documents]:
        try:
            session.check()
        except CollectionError:
            break
        eid = f"document-{len(result.documents) + 1:04d}"
        evidence = CollectedEvidence(
            id=eid,
            source=current,
            captured_at=now(),
            observed_fact="PDF could not be checked",
            available=False,
            unavailable_reason="Not collected",
        )
        document = DocumentObservation(requested_url=current, evidence_id=eid)
        result.evidence.append(evidence)
        result.documents.append(document)
        try:
            response = session.get(current, allowed)
            evidence.source = response.url
            evidence.captured_at = now()
            document.final_url = response.url
            document.http_status = response.status
            if not 200 <= response.status < 300:
                raise CollectionError(f"HTTP {response.status}")
            if not response.body.startswith(b"%PDF-"):
                raise CollectionError("Response is not a PDF")
            artifact = store.save(f"{eid}.pdf", response.body, "pdf")
            evidence.artifacts.append(artifact)
            evidence.artifact_path, evidence.sha256 = artifact.path, artifact.sha256
            evidence.observed_fact = "Public PDF downloaded; extraction is a technical observation"
            evidence.available = True
            evidence.unavailable_reason = None
            extracted = extract_inventory(
                store.root / artifact.path,
                limits.max_pdf_pages,
                min(limits.timeout_seconds, session.remaining()),
            )
            document.reading = extracted.reading
            document.reading.original_saved = True
            document.reading.source_sha256 = artifact.sha256
            document.page_texts = extracted.page_texts
            document.text, document.extraction_status, document.extraction_reason = legacy_result(
                extracted
            )
            if document.extraction_status == "failed":
                evidence.collection_errors.append(
                    document.extraction_reason or "PDF extraction failed"
                )
        except (CollectionError, OSError) as exc:
            evidence.collection_errors.append(str(exc)[:500])
        finally:
            finalize_evidence(evidence)
    summary = session.finish(
        "page_limit"
        if queue
        else "document_limit"
        if len(pdf_urls) > limits.max_documents
        else "queue_exhausted"
    )
    if session.stop_reason:
        result.warnings.append("Crawl stopped: " + summary["stop_reason"])
    result.warnings.extend(verify_integrity(result, store.root))
    result = CollectionResult.model_validate(result.model_dump())
    (store.root / "collection.json").write_text(
        result.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    return result
