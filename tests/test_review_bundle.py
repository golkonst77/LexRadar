"""Evidence integrity, conservative grouping and offline publication boundary."""

import copy
import hashlib
import json

import pytest

from lexradar.review_bundle import build, form_groups


def form(*, kind="tel", checked=None, coverage="complete", url="https://clinic.test/"):
    return {
        "id": "form-1",
        "url": url,
        "citation": {"url": url},
        "fields": [{"type": kind, "name": "field", "checked": checked}],
        "policy_links": [],
        "consent_links": [],
        "static_coverage": {"status": coverage},
    }


def test_grouping_preserves_consent_and_coverage_differences():
    a = form(kind="checkbox", checked=True)
    same = dict(a, id="form-2", url="https://clinic.test/other")
    unchecked = form(kind="checkbox", checked=False)
    partial = form(kind="checkbox", checked=True, coverage="partial")
    groups = form_groups([a, same, unchecked, partial, form(kind="search")])
    assert len(groups) == 4
    assert len(groups[0]["members"]) == 2
    assert groups[-1]["kind"] == "technical"
    assert form_groups([dict(a, fields=[{"type": "hidden"}])])[0]["kind"] == "technical"


@pytest.fixture
def saved(tmp_path):
    source = tmp_path / "saved"
    dossier = source / "dossier"
    (dossier / "artifacts").mkdir(parents=True)
    (source / "analysis").mkdir()
    raw = b'<form><input type="checkbox" checked="checked"></form><script>bad()</script>'
    artifact = {
        "path": "artifacts/page.html",
        "kind": "html",
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    (dossier / artifact["path"]).write_bytes(raw)
    collection = {
        "target_url": "https://clinic.test/",
        "started_at": "2026-10-15T00:00:00Z",
        "evidence": [{"id": "page-1", "source": "https://clinic.test/", "artifacts": [artifact]}],
    }
    (dossier / "collection.json").write_text(json.dumps(collection))
    (source / "analysis/primary.local.json").write_text(json.dumps({"forms": [form()]}))
    brief = {
        "url": collection["target_url"],
        "client": "<script>client</script>",
        "value": "Value",
        "scope": ["Scope"],
        "limitations": ["Limit", "Limit"],
        "findings": [
            {
                "evidence_id": "page-1",
                "kind": "html",
                "quote": "<script>bad()</script>",
                "title": "Title",
                "fact": "Fact",
                "question": "Question",
                "deliverable": "Deliverable",
            }
        ],
    }
    return source, brief, tmp_path / "result"


def test_bundle_is_offline_escaped_and_traceable(saved, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Network or paid transport used")

    monkeypatch.setattr("socket.getaddrinfo", forbidden)
    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("lexradar.auditors.provider.OpenRouterTransport.post", forbidden)
    source, brief, output = saved
    result = build(source, brief, output)
    assert result["client_release"] == "HOLD"
    assert not result["legal_violations_established"]
    assert result["limitations"] == ["Limit"]
    assert result["findings"][0]["line"] == 1
    for page in output.glob("*.html"):
        assert "<script>" not in page.read_text()
    assert "&lt;script&gt;bad()" in (output / "evidence.html").read_text()
    hashes = json.loads((output / "sha256.json").read_text())
    for name, sha in hashes.items():
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == sha
    with pytest.raises(FileExistsError):
        build(source, brief, output)


@pytest.mark.parametrize("failure", ["hash", "quote", "url", "escape", "symlink"])
def test_rejects_invalid_evidence_before_writing(saved, failure):
    source, original, output = saved
    brief = copy.deepcopy(original)
    artifact = source / "dossier/artifacts/page.html"
    if failure == "hash":
        artifact.write_text("changed")
    elif failure == "quote":
        brief["findings"][0]["quote"] = "invented"
    elif failure == "url":
        brief["url"] = "https://different.test/"
    elif failure == "symlink":
        data = artifact.read_bytes()
        artifact.unlink()
        outside = source.parent / "outside.html"
        outside.write_bytes(data)
        artifact.symlink_to(outside)
    else:
        path = source / "dossier/collection.json"
        c = json.loads(path.read_text())
        c["evidence"][0]["artifacts"][0]["path"] = "../outside.html"
        path.write_text(json.dumps(c))
    with pytest.raises(ValueError):
        build(source, brief, output)
    assert not output.exists()


def test_pdf_quote_requires_verified_page(saved):
    from pathlib import Path

    source, brief, output = saved
    raw = (Path(__file__).resolve().parents[1] / "examples/mvp-site/terms.pdf").read_bytes()
    artifact = {
        "path": "artifacts/terms.pdf",
        "kind": "pdf",
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    (source / "dossier/artifacts/terms.pdf").write_bytes(raw)
    path = source / "dossier/collection.json"
    collection = json.loads(path.read_text())
    collection["evidence"][0]["artifacts"] = [artifact]
    path.write_text(json.dumps(collection))
    brief["findings"][0].update(kind="pdf", quote="Synthetic order terms.")
    with pytest.raises(ValueError, match="positive page"):
        build(source, brief, output)
    assert not output.exists()
    brief["findings"][0]["page"] = 1
    result = build(source, brief, output)
    assert result["findings"][0]["basis"] == "pdf_text_extraction"
    assert result["findings"][0]["page"] == 1
    assert result["findings"][0]["line"] is None
