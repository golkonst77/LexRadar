"""Image acquisition and OCR-result states, not a claim of real scanned-text recognition."""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_collector import server as server  # existing bounded local HTTP fixture

from lexradar.collector import Limits, collect
from lexradar.collector.artifacts import verify_integrity
from lexradar.collector.network import Fetcher, Response
from lexradar.collector.ocr import run_image_ocr


def test_document_images_download_deduplicate_and_keep_failure(server, tmp_path, monkeypatch):
    url, policy, requests = server
    original = Path.read_bytes
    html = (
        "<html><head><title>Документы</title></head><body>"
        '<img src="/scan.png"><div data-original="/scan.png" data-img-zoom-url="/scan.png"></div>'
        '<div style="background-image:url(/unavailable.png)"></div>'
        '<img src="https://external.example/scan.png">'
        '<form id="hidden" style="display:none"><input name="phone"></form>'
        "</body></html>"
    ).encode()
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda p: html if p == Path("tests/fixtures/collector/index.html") else original(p),
    )
    limits = Limits(max_pages=1, max_documents=2)
    fetcher = Fetcher(limits, policy)
    get = fetcher.get
    image_requests = []

    def response(target, allowed, **kwargs):
        if target.endswith("/scan.png"):
            image_requests.append(target)
            return Response(target, 200, "image/png", b"synthetic image bytes, not an OCR fixture")
        return get(target, allowed, **kwargs)

    monkeypatch.setattr(fetcher, "get", response)
    output = tmp_path / "images"
    result = collect(url, output, limits, fetcher=fetcher)
    rows = result.pages[0].document_images
    assert len(rows) == 3 and len(image_requests) == 1
    saved = next(r for r in rows if r.download_status == "saved")
    assert len(saved.declarations) == 3 and saved.artifact.kind == "image"
    assert (output / saved.artifact.path).read_bytes().startswith(b"synthetic")
    assert {r.download_status for r in rows} == {"saved", "failed", "blocked"}
    assert all(r.reading_status == "not_read" for r in rows)
    assert result.pages[0].forms[0].visibility == "hidden_in_isolated_render"
    assert not result.pages[0].forms[0].popup_opened
    assert not verify_integrity(result, output)
    assert all("external.example" not in r for r in requests)


@pytest.mark.parametrize(
    "mode,status", [("timeout", "timeout"), ("error", "failed"), ("low", "low_confidence")]
)
def test_ocr_adapter_failure_and_low_confidence_sidecar(tmp_path, monkeypatch, mode, status):
    # Subprocess outcomes test state handling only. This is NOT an OCR smoke test.
    monkeypatch.setattr("lexradar.collector.ocr.shutil.which", lambda program: "/fixture/tesseract")

    def command(argv, **kwargs):
        if mode == "timeout":
            raise subprocess.TimeoutExpired(argv, 1)
        if mode == "error":
            raise subprocess.CalledProcessError(1, argv)
        prefix = Path(argv[2])
        prefix.with_suffix(".txt").write_text("Uncertain synthetic text", encoding="utf-8")
        prefix.with_suffix(".tsv").write_text(
            "header\n5\t1\t1\t1\t1\t1\t0\t0\t1\t1\t12\tUncertain\n", encoding="utf-8"
        )

    monkeypatch.setattr("lexradar.collector.ocr.subprocess.run", command)
    report = run_image_ocr(
        tmp_path,
        SimpleNamespace(id="page-1", source="https://fixture.example/documents"),
        SimpleNamespace(path="artifacts/scan.image", sha256="a" * 64),
        b"synthetic",
        b"{}",
        tmp_path / "ocr",
        "eng",
        1,
    )
    assert report["pages"][0]["status"] == status
    assert not report["ocr_complete"] and not report["client_release_allowed"]
    assert (tmp_path / "ocr/ocr.json").is_file()
