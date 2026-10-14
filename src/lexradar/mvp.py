"""URL to a local commercial draft. AI transport is always synthetic, never external."""

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from .auditors.compact_pipeline import analyze_compact
from .auditors.models import AnalysisConfig, ModelSettings
from .collector import CrawlOptions, Limits, collect
from .collector.network import CollectionError, Response, canonical_url, origin
from .commercial_preview import render_mvp

DEMO_URL = "https://demo.lexradar.example/"


class FixtureFetcher:
    """Exact bundled synthetic origin; never resolves DNS or opens sockets."""

    synthetic = True

    def __init__(self, root, limits):
        self.root = root.resolve()
        self.limits = limits
        self.received = 0

    def get(self, url, allowed_origin, *, before_request=None, on_event=None, **kwargs):
        if origin(url) != origin(DEMO_URL) or allowed_origin != origin(DEMO_URL):
            raise CollectionError("Fixture origin mismatch")
        if before_request:
            before_request(url)
        path = self.root / {
            "/": "index.html",
            "/policy.html": "policy.html",
            "/terms.pdf": "terms.pdf",
            "/robots.txt": "robots.txt",
        }.get(urlsplit(url).path, "missing")
        body = path.read_bytes() if path.is_file() else b""
        if len(body) > min(
            self.limits.max_file_bytes, kwargs.get("max_file_bytes") or self.limits.max_file_bytes
        ):
            raise CollectionError("Fixture file limit exceeded")
        if self.received + len(body) > self.limits.max_total_bytes:
            raise CollectionError("Fixture total limit exceeded")
        self.received += len(body)
        if on_event:
            on_event("fixture_read", url, 200 if body else 404)
        content_type = "application/pdf" if path.suffix == ".pdf" else "text/html"
        if path.suffix == ".txt":
            content_type = "text/plain"
        return Response(url, 200 if body else 404, content_type, body)


def run(url, output, *, fixture=None, static_only=False, as_of=None, max_pages=5):
    url = canonical_url(url)
    if fixture and url != DEMO_URL:
        raise ValueError("Offline fixture requires the exact demo URL")
    output.mkdir(parents=True, exist_ok=False)
    manifest = {
        "url": url,
        "fixture": bool(fixture),
        "static_only": static_only,
        "ai_mode": "offline",
        "request_sent": False,
        "cost_usd": "0",
        "client_release_allowed": False,
        "status": "started",
    }
    try:
        limits = Limits(max_pages=max_pages, max_documents=3, timeout_seconds=8)
        collection = collect(
            url,
            output / "dossier",
            limits,
            fetcher=FixtureFetcher(fixture, limits) if fixture else None,
            static_only=static_only,
            crawl_options=CrawlOptions(max_requests=20, max_duration_seconds=90),
        )
        manifest.update(
            pages=len(collection.pages),
            documents=len(collection.documents),
            available=sum(e.available for e in collection.evidence),
        )
        if not any(e.available for e in collection.evidence):
            raise ValueError("No available evidence; inspect dossier and crawl journal")
        settings = ModelSettings(
            model="synthetic/compact-a",
            prompt_price_cap="0.10",
            completion_price_cap="0.10",
            max_output_tokens=2000,
        )
        config = AnalysisConfig(
            auditor_a=settings,
            auditor_b=settings,
            single_auditor=True,
            max_budget_usd="0.05",
            retries=0,
        )
        (output / "config.json").write_text(config.model_dump_json(indent=2))
        report = analyze_compact(
            output / "dossier",
            output / "analysis",
            config,
            as_of=as_of or datetime.now(UTC),
            approval_template=False,
        )
        manifest["analysis_status"] = report["status"]
        if report["status"] != "success":
            raise ValueError("Compact analysis failed; inspect saved analysis")
        render_mvp(output, output / "proposal.html", fixture=bool(fixture))
        manifest["status"] = "success"
    except Exception as exc:
        manifest.update(status="failed", error_type=type(exc).__name__, error=str(exc)[:2000])
        raise
    finally:
        (output / "run.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        hashes = {
            str(p.relative_to(output)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(output.rglob("*"))
            if p.is_file() and p.name != "sha256.json"
        }
        (output / "sha256.json").write_text(json.dumps(hashes, indent=2))
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--static-only", action="store_true")
    parser.add_argument("--as-of", type=datetime.fromisoformat)
    parser.add_argument("--max-pages", type=int, default=5)
    args = parser.parse_args(argv)
    try:
        result = run(
            args.url,
            args.output,
            fixture=args.fixture,
            static_only=args.static_only,
            as_of=args.as_of,
            max_pages=args.max_pages,
        )
    except Exception as exc:
        parser.exit(1, f"MVP failed ({type(exc).__name__}); inspect output/run.json\n")
    print(f"{result['status']}; AI simulated; USD 0; HTML: {args.output / 'proposal.html'}")
