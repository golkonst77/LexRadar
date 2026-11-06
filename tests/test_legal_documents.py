from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from test_collector import pdf_bytes
from test_policy5 import packet

from lexradar.collector import collect, Limits, CrawlOptions
from lexradar.collector.network import Response, canonical_url
from lexradar.collector.extract import legal_link_kind
from lexradar.collector.reading import extract_html
from lexradar.policy5 import inspect_policy5
from lexradar.primary.markup import static_reference


class FixtureFetcher:
    synthetic = True

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def get(self, url, allowed_origin, **kwargs):
        if kwargs.get("before_request"):
            kwargs["before_request"](url)
        self.requests.append(url)
        if kwargs.get("on_event"):
            kwargs["on_event"]("request_sent", url)
        path = urlsplit(url).path
        if path == "/robots.txt":
            return Response(url, 200, "text/plain", b"User-agent: LexRadar\nDisallow:\n")
        body = self.routes.get(path, b"<html><body>Fixture</body></html>")
        return Response(url, 200, "application/pdf" if path.endswith(".pdf") else "text/html", body)


def test_uri_spaces_unicode_and_document_types():
    url = static_reference("https://clinic.test/about/legal/", "../../files/Политика клиники.pdf")
    assert "%20" in url and " " not in url and canonical_url(url) == url
    assert legal_link_kind(url, "Политика обработки персональных данных") == "privacy_policy"
    assert (
        legal_link_kind("/about/agreement/", "Согласие на обработку персональных данных")
        == "consent"
    )
    assert (
        legal_link_kind("/about/legal-information/", "Правовая информация") == "legal_information"
    )
    assert legal_link_kind("/lipolitiki/", "Липолитики") is None


def test_screening_fetches_legal_pages_and_their_pdf_before_stopping(tmp_path):
    routes = {
        "/": (
            '<html><body><form><input type="tel"><a href="/consent">Согласие на обработку данных</a>'
            '</form><footer><a href="/privacy">Политика конфиденциальности</a>'
            '<a href="/about/legal-information/">Правовая информация</a></footer></body></html>'
        ).encode(),
        "/about/legal-information/": (
            '<html><body><a href="/files/a%20policy.pdf">Политика обработки данных ООО А</a>'
            "</body></html>"
        ).encode(),
        "/files/a%20policy.pdf": pdf_bytes(True),
    }
    f = FixtureFetcher(routes)
    result = collect(
        "https://clinic.test/",
        tmp_path / "run",
        Limits(max_pages=5, max_documents=1),
        fetcher=f,
        static_only=True,
        screening_mode=True,
        crawl_options=CrawlOptions(min_delay_seconds=0.1, max_requests=10, max_duration_seconds=10),
    )
    assert any(str(p.requested_url).endswith("/privacy") for p in result.pages)
    assert any(str(p.requested_url).endswith("/consent") for p in result.pages)
    assert len(result.documents) == 1 and result.documents[0].text
    assert "https://clinic.test/files/a%20policy.pdf" in f.requests


def test_two_html_hops_only(tmp_path):
    routes = {
        f"/privacy{n}": f'<html><body><a href="/privacy{n + 1}">Политика конфиденциальности</a></body></html>'.encode()
        for n in range(4)
    }
    f = FixtureFetcher(routes)
    result = collect(
        "https://clinic.test/privacy0",
        tmp_path / "depth",
        Limits(max_pages=8, max_documents=0),
        fetcher=f,
        static_only=True,
        crawl_options=CrawlOptions(min_delay_seconds=0.1, max_requests=10, max_duration_seconds=10),
    )
    assert len(result.pages) == 3
    assert not any("/privacy3" in u for u in f.requests)


def test_document_main_complete_despite_broken_navigation():
    raw = b"<html><body><div><span></div><main><p>Full policy text</p></main></body></html>"
    original, reading = extract_html(raw, 10000)
    text, scoped = extract_html(raw, 10000, main_only=True)
    assert reading.extraction_state == "partial"
    assert text == "Full policy text" and scoped.extraction_state == "complete"
    assert "Complete static main content; outer navigation excluded" in scoped.limitations
    fake = b"<script><main><p>Fake policy</p></main></script><div><span></div>"
    assert "Fake policy" not in extract_html(fake, 10000, main_only=True)[0]


def test_omitted_head_end_is_valid_at_explicit_body():
    text, reading = extract_html(
        b"<html><head><title>X</title><body><p>Full policy</p></body></html>", 1000
    )
    assert text == "Full policy" and reading.extraction_state == "complete"


def test_policy_checks_do_not_pool_two_operators():
    a = packet(pdf=True, missing="retention")
    b = packet(pdf=True, url="https://clinic.test/second.pdf")
    pa = a.data.documents[0]
    pb = b.data.documents[0]
    pa.evidence_id = "a"
    pb.evidence_id = "b"
    pa.text = pa.text.replace("Клиника", "Первый")
    pb.text = pb.text.replace("Клиника", "Второй")
    a.data.documents = [pa, pb]
    a.data.pages[0].relevant_links += b.data.pages[0].relevant_links
    a.data.evidence = [
        SimpleNamespace(id="home", available=True, artifacts=[]),
        SimpleNamespace(id="a", available=True, artifacts=[]),
        SimpleNamespace(id="b", available=True, artifacts=[]),
    ]
    rows = inspect_policy5(a)
    assert len(rows) == 10
    first = [r for r in rows if r["source"] == str(pa.requested_url)]
    second = [r for r in rows if r["source"] == str(pb.requested_url)]
    assert len(first) == len(second) == 5
    assert next(r for r in first if r["check"] == "retention")["status"] == "FAIL"
    assert all(r["status"] == "PASS" for r in second)


def test_footer_policy_is_tried_before_menu_fallback(tmp_path):
    routes = {
        "/": (
            '<html><body><a href="/aaa-privacy">Политика конфиденциальности</a>'
            '<footer><a href="/zzz-privacy">Политика конфиденциальности</a></footer>'
            "</body></html>"
        ).encode()
    }
    f = FixtureFetcher(routes)
    collect(
        "https://clinic.test/",
        tmp_path / "footer",
        Limits(max_pages=2, max_documents=0),
        fetcher=f,
        static_only=True,
        screening_mode=True,
        crawl_options=CrawlOptions(min_delay_seconds=0.1, max_requests=5, max_duration_seconds=10),
    )
    assert "https://clinic.test/zzz-privacy" in f.requests
    assert "https://clinic.test/aaa-privacy" not in f.requests


def test_specific_goals_heading_and_conditional_destruction_are_understood():
    p = packet()
    text = p.data.pages[0].text.replace(
        "Сведения используются, чтобы оформить запись на приём и ответить посетителю.",
        "Цели сбора персональной информации пользователя: установление обратной связи с посетителем.",
    )
    text = text.replace(
        "Когда ответ отправлен, записи стираются из системы.",
        "Пользователь вправе требовать уничтожения в случае, если данные больше не нужны для обработки.",
    )
    p.data.pages[0].text = text
    rows = inspect_policy5(p)
    assert next(r for r in rows if r["check"] == "purposes")["status"] == "PASS"
    assert next(r for r in rows if r["check"] == "deletion")["status"] == "PASS"


def test_destruction_condition_after_rights_heading_with_colon():
    p=packet()
    p.data.pages[0].text=p.data.pages[0].text.replace(
        'Когда ответ отправлен, записи стираются из системы.',
        'Субъект имеет право: требовать уничтожения в случае, если данные больше не нужны.')
    assert next(r for r in inspect_policy5(p) if r['check']=='deletion')['status']=='PASS'
