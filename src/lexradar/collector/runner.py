"""Sequential bounded crawl. Browser renders fetched bytes with all networking disabled."""

import os
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Error as BrowserError
from playwright.sync_api import sync_playwright

from .artifacts import ArtifactStore, verify_integrity
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
from .pdf import extract_pdf


def now():
    return datetime.now(UTC)


def collect(
    url: str, output: Path, limits: Limits | None = None, *, fetcher: Fetcher | None = None
) -> CollectionResult:
    limits = limits or Limits()
    url = canonical_url(url)
    allowed = origin(url)
    store = ArtifactStore(output, limits)
    fetcher = fetcher or Fetcher(limits)
    result = CollectionResult(target_url=url, started_at=now(), limits=limits)
    queue = deque([url])
    seen = {url}
    document_urls = set()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ.get("LEXRADAR_CHROMIUM_PATH")
        )
        context = browser.new_context(
            java_script_enabled=False,
            service_workers="block",
            accept_downloads=False,
            viewport={"width": 1280, "height": 900},
        )
        try:
            while queue and len(result.pages) < limits.max_pages:
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
                    response = fetcher.get(current, allowed)
                    evidence.source = response.url
                    evidence.captured_at = now()
                    page_record.captured_at = evidence.captured_at
                    page_record.final_url = response.url
                    page_record.http_status = response.status
                    if response.status >= 400:
                        raise CollectionError(f"HTTP {response.status}")
                    if "text/html" not in response.content_type.lower():
                        raise CollectionError("Page is not HTML")
                    evidence.artifacts.append(store.save(f"{eid}.html", response.body, "html"))
                    page = context.new_page()
                    page.set_default_timeout(limits.timeout_seconds * 1000)
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
                    page_record.text = dom["text"][: limits.max_file_bytes // 4]
                    page_record.categories = categories(current + " " + page_record.title)
                    evidence.artifacts.append(
                        store.save(f"{eid}.txt", page_record.text.encode(), "text")
                    )
                    evidence.artifacts.append(
                        store.save(f"{eid}.png", page.screenshot(), "screenshot")
                    )
                    result.entities.extend(entities(page_record.text, response.url))
                    for index, form in enumerate(dom["forms"]):
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
                        except (CollectionError, BrowserError) as exc:
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
                    evidence.observed_fact = (
                        "HTML page and visible content captured; no legal conclusion"
                    )
                    evidence.available = True
                    evidence.unavailable_reason = None
                except (CollectionError, BrowserError, OSError) as exc:
                    evidence.unavailable_reason = str(exc)[:500]
                finally:
                    if page:
                        page.close()
                    if evidence.artifacts:
                        evidence.artifact_path = evidence.artifacts[0].path
                        evidence.sha256 = evidence.artifacts[0].sha256
            if queue:
                result.warnings.append("Page crawl limit reached")
        finally:
            context.close()
            browser.close()
    pdf_urls = [u for u in sorted(document_urls) if urlsplit(u).path.lower().endswith(".pdf")]
    if len(pdf_urls) > limits.max_documents:
        result.warnings.append("PDF download limit reached")
    for current in pdf_urls[: limits.max_documents]:
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
            response = fetcher.get(current, allowed)
            evidence.source = response.url
            evidence.captured_at = now()
            document.final_url = response.url
            document.http_status = response.status
            if response.status >= 400:
                raise CollectionError(f"HTTP {response.status}")
            if not response.body.startswith(b"%PDF-"):
                raise CollectionError("Response is not a PDF")
            artifact = store.save(f"{eid}.pdf", response.body, "pdf")
            evidence.artifacts.append(artifact)
            evidence.artifact_path, evidence.sha256 = artifact.path, artifact.sha256
            evidence.observed_fact = "Public PDF downloaded; extraction is a technical observation"
            evidence.available = True
            evidence.unavailable_reason = None
            document.text, document.extraction_status, document.extraction_reason = extract_pdf(
                store.root / artifact.path, limits.max_pdf_pages, limits.timeout_seconds
            )
        except (CollectionError, OSError) as exc:
            evidence.unavailable_reason = str(exc)[:500]
    result.warnings.extend(verify_integrity(result, store.root))
    (store.root / "collection.json").write_text(
        result.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    return result
