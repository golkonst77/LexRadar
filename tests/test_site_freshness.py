"""Local passive-signal check; no network or browser."""

from datetime import UTC, datetime

from lexradar.collector.freshness import site_freshness


def test_site_freshness():
    result = site_freshness(
        '<main>© 2025</main><footer>Сайт создан в 2018. '
        '&copy; <span>2018–2024</span><script>© 2099</script></footer>'.encode(),
        'Wed, 01 Jan 2020 00:00:00 GMT', '"abc"', 'https://example.test/',
    )
    assert result.footer_year == 2024
    assert result.footer_year_stale == (2024 < datetime.now(UTC).year)
    assert result.http_etag == '"abc"'
    assert result.http_last_modified == 'Wed, 01 Jan 2020 00:00:00 GMT'
    assert result.signal_source == 'https://example.test/'
    assert all(value is None for value in site_freshness(
        b'<footer>Site created in 2018</footer>'
    ).model_dump().values())
    current = datetime.now(UTC).year
    assert site_freshness(f'<footer>© {current}</footer>'.encode()).footer_year_stale is False
    assert site_freshness(b'<div class="site-footer">&#169; 2022</div>').footer_year == 2022
    print(result.model_dump_json(indent=2))
