"""Robots failures must not prevent HTML collection; recognized denials still apply."""

import pytest

from lexradar.collector.crawl import CrawlSession
from lexradar.collector.network import CollectionError, Response, origin
from lexradar.collector.robots import parse_robots


@pytest.mark.parametrize("robots", [
    Response("https://dmg.clinic/robots.txt", 200, "text/plain", b"Sitemap: https://dmg.clinic/sitemap.xml"),
    Response("https://dmg.clinic/robots.txt", 200, "text/html", b"<html>Error</html>"),
    Response("https://dmg.clinic/robots.txt", 200, "text/plain", b"\xffinvalid"),
    Response("https://dmg.clinic/robots.txt", 404, "text/plain", b""),
    Response("https://dmg.clinic/robots.txt", 503, "text/plain", b"Error"),
    CollectionError("Request timeout"),
    OSError("Connection error"),
])
def test_robots_failure_continues_html(tmp_path, robots):
    class Fetcher:
        def get(self, url, allowed_origin, *, before_request, **kwargs):
            before_request(url)
            if url.endswith("robots.txt"):
                if isinstance(robots, Exception):
                    raise robots
                return robots
            return Response(url, 200, "text/html", b"<html>Clinic</html>")

    url = "https://dmg.clinic/"
    session = CrawlSession(Fetcher(), url, tmp_path)
    assert session.get(url, origin(url)).status == 200
    assert session.finish()["stop_reason"] == "queue_exhausted"


@pytest.mark.parametrize("body", [
    b"User-agent: *\nDisallow: /private\n",
    b"User-agent: *\nDisallow: /private\nbroken line\nAllow: malformed\nCrawl-delay: bad\n",
    b"User-agent: *\nDisallow: /private\nAllow: /bad%zz\n",
])
def test_recovery_keeps_recognized_denials(tmp_path, body):
    class Fetcher:
        def get(self, url, allowed_origin, *, before_request, **kwargs):
            before_request(url)
            assert url.endswith("robots.txt"), "Denied content must never be fetched"
            return Response(url, 200, "text/plain", body)

    url = "https://dmg.clinic/"
    session = CrawlSession(Fetcher(), url, tmp_path)
    with pytest.raises(CollectionError, match="robots_disallow"):
        session.get(url + "private", origin(url))
    assert parse_robots(body, recover=True).allows(url)
