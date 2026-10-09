"""Relative artifact paths and integrity checks for reviewable dossiers."""

import hashlib
from pathlib import Path

from .models import SCREENSHOT_NOTICE, Artifact, CollectionResult, Limits
from .network import CollectionError


class ArtifactStore:
    def __init__(self, root: Path, limits: Limits):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=False)
        (self.root / "artifacts").mkdir()
        self.limits = limits
        self.saved = 0

    def save(self, name: str, content: bytes, kind: str) -> Artifact:
        if len(content) > self.limits.max_file_bytes:
            raise CollectionError("Artifact file size limit exceeded")
        if self.saved + len(content) > self.limits.max_total_bytes:
            raise CollectionError("Total artifact limit exceeded")
        path = self.root / "artifacts" / name
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            temporary.write_bytes(content)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        self.saved += len(content)
        return Artifact(
            path=path.relative_to(self.root).as_posix(),
            sha256=hashlib.sha256(content).hexdigest(),
            size=len(content),
            kind=kind,
            representation={
                "html": "original_html",
                "text": "extracted_text",
                "pdf": "original_pdf",
                "screenshot": "isolated_screenshot",
                "form_screenshot": "isolated_screenshot",
            }[kind],
            notice=SCREENSHOT_NOTICE if kind in {"screenshot", "form_screenshot"} else None,
        )


def verify_integrity(result: CollectionResult, root: Path) -> list[str]:
    issues = []
    root = root.resolve()
    for evidence in result.evidence:
        kinds = {a.kind for a in evidence.artifacts}
        expected = {"html", "screenshot", "text"} if evidence.id.startswith("page-") else {"pdf"}
        if evidence.status == "complete" and not expected <= kinds:
            issues.append(f"{evidence.id}: missing mandatory artifact")
        for artifact in evidence.artifacts:
            path = (root / artifact.path).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                issues.append(f"{evidence.id}: missing or unsafe artifact {artifact.path}")
                continue
            if path.stat().st_size > result.limits.max_file_bytes:
                issues.append(f"{evidence.id}: artifact exceeds file limit {artifact.path}")
                continue
            content = path.read_bytes()
            if (
                len(content) != artifact.size
                or hashlib.sha256(content).hexdigest() != artifact.sha256
            ):
                issues.append(f"{evidence.id}: integrity mismatch {artifact.path}")
        if evidence.artifacts and (
            evidence.artifact_path != evidence.artifacts[0].path
            or evidence.sha256 != evidence.artifacts[0].sha256
        ):
            issues.append(f"{evidence.id}: primary artifact mismatch")
    for page in result.pages:
        for form in page.forms:
            if form.screenshot is not None and not any(
                form.screenshot in evidence.artifacts
                for evidence in result.evidence
                if evidence.id == page.evidence_id
            ):
                issues.append(f"{page.evidence_id}: unregistered form screenshot")
            if form.screenshot is None and not form.unavailable_reason:
                issues.append(f"{page.evidence_id}: missing form screenshot")
    return issues
