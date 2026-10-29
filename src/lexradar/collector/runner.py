"""Sequential bounded crawl. Browser renders fetched bytes with all networking disabled."""

import json
import os
import re
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Error as BrowserError
from playwright.sync_api import sync_playwright

from .artifacts import ArtifactStore, verify_integrity
from .crawl import CrawlOptions, CrawlSession
from .document_priority import document_hints, document_sort_key
from .document_priority import document_priority as document_priority
from .extract import (
    COOKIE_TEXT,
    DOCUMENT_EXTENSIONS,
    DOM_SCRIPT,
    categories,
    entities,
    legal_link_kind,
    screening_link_kind,
    screening_link_priority,
    static_analytics_hints,
)
from .models import (
    CollectedEvidence,
    CollectionResult,
    DocumentObservation,
    FormObservation,
    Limits,
    PageObservation,
    RelevantLinkObservation,
)
from .network import CollectionError, Fetcher, canonical_url, origin
from .pdf import extract_inventory, legacy_result
from .provenance import MaterialOrigin
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


def screening_signal(page: PageObservation) -> bool:
    has_contact_field = any(
        field.type.casefold() in {"tel", "email"}
        or re.search(r"phone|mobile|email|e-mail|телефон|почт|имя", field.name or "", re.I)
        or re.search(r"phone|mobile|email|e-mail|телефон|почт|имя", field.label or "", re.I)
        for form in page.forms
        for field in form.fields
    )
    has_disclosure = any(
        link.visible_in_static_html and link.kind in {"privacy_policy", "consent"}
        for link in page.relevant_links
    )
    return has_contact_field and has_disclosure


def collect(
    url: str,
    output: Path,
    limits: Limits | None = None,
    *,
    fetcher: Fetcher | None = None,
    crawl_options: CrawlOptions | None = None,
    static_only: bool = False,
    screening_mode: bool = False,
) -> CollectionResult:
    limits = limits or Limits()
    if screening_mode:
        limits = limits.model_copy(update={"max_pages": min(limits.max_pages, 7)})
    url = canonical_url(url)
    allowed = origin(url)
    store = ArtifactStore(output, limits)
    session = CrawlSession(fetcher or Fetcher(limits), url, store.root, crawl_options)
    try:
        return _collect(url, limits, allowed, store, session, static_only, screening_mode)
    except BaseException:
        session.finish("collector_error")
        raise


def _collect(url, limits, allowed, store, session, static_only=False, screening_mode=False):
    acquisition = MaterialOrigin(
        kind="collector",
        acquisition_method="bounded_static_http_collector",
        declared_source=url,
        captured_at_role="collection_observation",
        started_at_role="collector_run",
        limitations=[
            "Collector timestamps are local observations, not server response dates",
            "Local acquisition metadata does not authenticate publisher identity",
        ],
    )
    if getattr(session.fetcher, "synthetic", False):
        acquisition.acquisition_method = "synthetic_local_fixture_no_http"
        acquisition.limitations.append(
            "Synthetic fixture: no public website or HTTP response observed"
        )
    result = CollectionResult(target_url=url, started_at=now(), limits=limits, origin=acquisition)
    queue = deque([url])
    seen = {url}
    depths = {url: 0}
    page_priorities = {}
    document_urls: dict[str, list[str]] = {}
    from .static_renderer import static_playwright

    if static_only:
        result.warnings.append("Static mode: no screenshots, scripts or dynamic behavior checked")
    if screening_mode:
        result.warnings.append(
            "Express screening is static only; missing elements are preliminary observations, "
            "not proof of absence or a violation. Link visibility excludes external CSS and JS."
        )
    target_pages = min(5, limits.max_pages)
    signal_found = False
    early_stop = False
    target_stop = False

    def link_key(link):
        if link.get("footer_policy"):
            return (-2, 0, link["url"]) if not screening_mode else (-2, link["url"])
        if screening_mode:
            return (-1 if legal_link_kind(link["url"], link["text"]) else
                    screening_link_priority(link["text"] + " " + link["url"])), link["url"]
        kind = legal_link_kind(link["url"], link["text"])
        priority = {"privacy_policy": 0, "consent": 1, "legal_information": 2, "document": 3}
        hints = document_sort_key(link["url"], document_hints(link["url"], link["text"]))
        return (0 if kind else 1, priority.get(kind, hints[0] * 10 + hints[1]), link["url"])

    with static_playwright() if static_only else sync_playwright() as playwright:
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
                page_priorities.pop(current, None)
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
                    requested_url=current,
                    captured_at=timestamp,
                    evidence_id=eid,
                    origin=acquisition.model_copy(update={"declared_source": current}),
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
                    if screening_mode:
                        page_record.static_analytics_hints = static_analytics_hints(response.body)
                        page_record.cookie_text_observed = bool(
                            COOKIE_TEXT.search(response.body.decode("utf-8", errors="replace"))
                        )
                        page_record.consent_management_controls = dom.get("consentControls", [])
                    page_record.text, page_record.reading = extract_html(
                        response.body, min(limits.max_file_bytes // 4, 1_000_000),
                        main_only=legal_link_kind(current, page_record.title) in
                        {"privacy_policy", "consent", "legal_information"},
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
                    if screening_mode or static_only:
                        for link in dom["links"]:
                            kind = legal_link_kind(link["url"], link["text"]) or screening_link_kind(link["text"] + " " + link["url"])
                            if kind:
                                page_record.relevant_links.append(
                                    RelevantLinkObservation(
                                        url=link["url"],
                                        text=link["text"],
                                        kind=kind,
                                        visible_in_static_html=bool(link.get("visible", True)),
                                    )
                                )
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
                    links = sorted(dom["links"], key=link_key)
                    unique_links = {}
                    for link in links:
                        unique_links.setdefault(link["url"], link)
                    links = list(unique_links.values())
                    for link in links[: limits.max_links_per_page]:
                        try:
                            target = canonical_url(link["url"])
                            if origin(target) != allowed:
                                continue
                            if urlsplit(target).path.lower().endswith(
                                (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".avif")
                            ):
                                continue
                            is_document = (
                                urlsplit(target).path.lower().endswith(DOCUMENT_EXTENSIONS)
                            )
                            if is_document:
                                page_record.document_links.append(target)
                                hints = document_hints(
                                    target, link["text"], page_record.title + " " + current
                                )
                                document_urls[target] = sorted(
                                    set(document_urls.get(target, [])) | set(hints)
                                )
                            elif (target not in seen and len(seen) < limits.max_pages * 10
                                  and depths[current] < 2):
                                seen.add(target)
                                depths[target] = depths[current] + 1
                                if screening_mode:
                                    page_priorities[target] = link_key(link)[0]
                                # Put legally relevant links ahead of ordinary navigation
                                # across the entire crawl, not just within this page.
                                queue.append(target)
                        except CollectionError:
                            continue
                    # Re-sort globally without appendleft reversing the policy-first order.
                    priorities = {link["url"]: link_key(link) for link in links}
                    for queued in queue:
                        priorities.setdefault(queued, (page_priorities[queued], queued)
                                              if screening_mode and queued in page_priorities
                                              else link_key({"url": queued, "text": ""}))
                    queue = deque(sorted(queue, key=lambda target: priorities[target]))
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
                if screening_mode:
                    signal_found = signal_found or screening_signal(page_record)
                    if queue and signal_found and not any(
                        page_priorities.get(queued, 4) < 0 for queued in queue
                    ):
                        early_stop = True
                        break
                    if queue and len(result.pages) >= target_pages and not any(
                        page_priorities.get(queued, 4) < 4 for queued in queue
                    ):
                        target_stop = True
                        break
            if queue and not early_stop and not target_stop:
                result.warnings.append("Page crawl limit reached")
            if target_stop:
                result.warnings.append(
                    "Express screening reached its 5-page target; remaining unprioritized pages "
                    "were not collected."
                )
        finally:
            for close in (context.close, browser.close):
                try:
                    close()
                except BrowserError as exc:
                    result.warnings.append(f"Browser cleanup failed: {str(exc)[:300]}")
    pdf_urls = sorted(
        (u for u in document_urls if urlsplit(u).path.lower().endswith(".pdf")),
        key=lambda u: document_sort_key(u, document_urls[u]),
    )
    selected = set(pdf_urls[: limits.max_documents])
    for candidate in pdf_urls:
        session.event(
            "pdf_priority",
            url=candidate,
            hints=document_urls[candidate],
            selected=candidate in selected,
            reason="selected_by_disclosure_hints" if candidate in selected else "document_limit",
        )
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
        document = DocumentObservation(
            requested_url=current,
            evidence_id=eid,
            origin=result.origin.model_copy(update={"declared_source": current}),
        )
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
                limits.max_pdf_page_chars,
                limits.max_pdf_total_chars,
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
        "screening_signal_found"
        if early_stop
        else "screening_target_reached"
        if target_stop
        else "page_limit"
        if queue
        else "document_limit"
        if len(pdf_urls) > limits.max_documents
        else "queue_exhausted"
    )
    if screening_mode:
        evidence_by_id = {e.id: e for e in result.evidence}
        summary.update(
            express_screening=True,
            target_pages=target_pages,
            page_limit=limits.max_pages,
            pages_attempted=len(result.pages),
            pages_html_captured=sum(
                any(a.kind == "html" for a in evidence_by_id[p.evidence_id].artifacts)
                for p in result.pages
            ),
            static_signal_found=signal_found,
        )
        (store.root / "crawl/summary.json").write_text(
            json.dumps(summary, indent=2) + "\n", encoding="utf-8"
        )
        if early_stop:
            result.warnings.append(
                "Express screening stopped early after observing a contact form and a "
                "policy/consent link; this is not a legal finding."
            )
    if session.stop_reason:
        result.warnings.append("Crawl stopped: " + summary["stop_reason"])
    result.warnings.extend(verify_integrity(result, store.root))
    result = CollectionResult.model_validate(result.model_dump())
    (store.root / "collection.json").write_text(
        result.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    return result
