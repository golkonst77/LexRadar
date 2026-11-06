"""Offline HTTP wire regressions; no public-site requests."""
import gzip
import http.client
import io
import zlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from lexradar.collector import network
from lexradar.collector.models import Limits
from lexradar.mvp_batch import collection_skip_reason


def fetch(monkeypatch, body, encoding='identity', status=200, limits=None):
    wire = (f'HTTP/1.1 {status} Test\r\nContent-Length: {len(body)}\r\n'
            f'Content-Type: text/html\r\nContent-Encoding: {encoding}\r\n\r\n').encode() + body
    peer = io.BytesIO(wire)
    peer._sock = SimpleNamespace(settimeout=lambda timeout: None)
    response = http.client.HTTPResponse(SimpleNamespace(makefile=lambda *args: io.BufferedReader(peer)))
    response.begin()
    connection = MagicMock()
    connection.getresponse.return_value = response
    connection.close.side_effect = response.close
    monkeypatch.setattr(network, 'PinnedHTTPS', lambda *args: connection)
    policy = SimpleNamespace(resolve=lambda *args, **kwargs: '8.8.8.8')
    fetcher = network.Fetcher(limits or Limits(), policy)
    result = fetcher.get('https://fixture.example/', network.origin('https://fixture.example/'))
    headers = connection.request.call_args.kwargs['headers']
    assert 'text/html' in headers['Accept'] and 'gzip' in headers['Accept-Encoding']
    return result, fetcher


@pytest.mark.parametrize('encoding,encode', [('identity', bytes), ('gzip', gzip.compress), ('deflate', zlib.compress)])
def test_decoding(monkeypatch, encoding, encode):
    body = b'<html>Public page</html>' * 20
    result, fetcher = fetch(monkeypatch, encode(body), encoding)
    assert result.body == body
    assert fetcher.received == max(len(body), len(encode(body)))


@pytest.mark.parametrize('encoding', ['gzip', 'deflate'])
def test_decoded_size_bound(monkeypatch, encoding):
    encode = gzip.compress if encoding == 'gzip' else zlib.compress
    with pytest.raises(network.CollectionError, match='Decoded response size limit'):
        fetch(monkeypatch, encode(b'a' * 1025), encoding, limits=Limits(max_file_bytes=1024))
    with pytest.raises(network.CollectionError, match='Decoded response size limit'):
        fetch(monkeypatch, encode(b'a' * 1025), encoding, limits=Limits(max_total_bytes=1024))


@pytest.mark.parametrize('body', [b'bad', gzip.compress(b'page')[:-1], gzip.compress(b'page') + b'trailing'])
def test_corrupt_compression(monkeypatch, body):
    with pytest.raises(network.CollectionError, match='compressed response'):
        fetch(monkeypatch, body, 'gzip')


@pytest.mark.parametrize('status', [403, 415, 429, 503])
def test_status_preserved_without_decoding_or_retry(monkeypatch, status):
    result, _ = fetch(monkeypatch, b'bad encoded error', 'br', status)
    assert result.status == status and result.body == b''


def test_unknown_encoding(monkeypatch):
    with pytest.raises(network.CollectionError, match='Unsupported Content-Encoding: br'):
        fetch(monkeypatch, b'unknown', 'br')


@pytest.mark.parametrize('reason,description', [
    ('HTTP 403', 'Доступ запрещён'), ('HTTP 415', 'формат HTTP-запроса'),
    ('Fetch failed: SSLCertVerificationError', 'TLS-сертификат'),
    ('crawl_duration_limit', 'лимит времени'),
])
def test_hold_reason(reason, description):
    collection = SimpleNamespace(evidence=[SimpleNamespace(collection_errors=[reason])])
    assert description in collection_skip_reason(collection)
    assert reason in collection_skip_reason(collection)
