"""Bounded first-pass queue. Ranking is not a legal decision or a paid-work approval."""

import argparse
import concurrent.futures
import fcntl
import hashlib
import html
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field

from .collector.network import CollectionError, canonical_url
from .models import Model


class BatchOptions(Model):
    workers: int = Field(default=1, ge=1, le=4)
    max_sites: int = Field(default=1000, ge=1, le=1000)
    site_timeout: float = Field(default=180, ge=1, le=900, allow_inf_nan=False)
    max_pages: int = Field(default=3, ge=1, le=10)
    max_documents: int = Field(default=2, ge=0, le=10)
    max_requests: int = Field(default=16, ge=1, le=64)
    crawl_duration: float = Field(default=60, ge=1, le=300, allow_inf_nan=False)
    min_delay: float = Field(default=2, ge=2, le=60, allow_inf_nan=False)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def safe_path(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Symlink batch path rejected")
    return path


def write_json(path, value):
    safe_path(path)
    temporary = path.with_suffix(".tmp")
    safe_path(temporary)
    with temporary.open("w") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    temporary.replace(path)


def snapshot(root):
    """Bind actual files, reject symlinks; never reuse altered results on resume."""
    result = {}
    total = 0
    for path in sorted(root.rglob("*")):
        safe_path(path)
        if path.is_file():
            total += path.stat().st_size
            if total > 250_000_000:
                raise ValueError("Site output exceeds batch snapshot bound")
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def kill_descendants(pid):
    # Linux worker supervision: Chromium/other children may create their own group.
    processes = {}
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = stat.read_text().rsplit(")", 1)[1].split()
            processes[int(stat.parent.name)] = (int(fields[1]), fields[19])
        except (OSError, ValueError, IndexError):
            continue
    parents = {pid}
    children = []
    while True:
        found = [
            child
            for child, (parent, _) in processes.items()
            if parent in parents and child not in parents
        ]
        if not found:
            break
        children.extend(found)
        parents.update(found)
    for child in reversed(children):
        try:
            fields = Path(f"/proc/{child}/stat").read_text().rsplit(")", 1)[1].split()
            if fields[19] == processes[child][1]:
                os.kill(child, signal.SIGKILL)
        except (OSError, IndexError):
            pass


def stop_previous_worker(directory):
    record = directory / "worker.pid.json"
    if not record.exists():
        return
    safe_path(record)
    values = json.loads(record.read_text())
    pid = values["pid"]
    if not isinstance(pid, int) or pid <= 1:
        raise ValueError("Invalid saved worker PID")
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        command_line = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except FileNotFoundError:
        return
    expected = [os.fsencode(sys.executable), b"-m", b"lexradar.batch", os.fsencode(directory)]
    if fields[19] != values["start_ticks"] or command_line[:4] != expected:
        return  # PID reused or unrelated process: never kill from an editable PID alone.
    kill_descendants(pid)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_process(argv, log, timeout, *, new_session=True):
    # The module has no __main__; invoke the same installed CLI entrypoint explicitly.
    with log.open("wb") as file:
        process = subprocess.Popen(
            argv, stdout=file, stderr=subprocess.STDOUT, start_new_session=new_session
        )
        try:
            if new_session and log.name == "worker.log":
                fields = Path(f"/proc/{process.pid}/stat").read_text().rsplit(")", 1)[1].split()
                write_json(
                    log.parent / "worker.pid.json", {"pid": process.pid, "start_ticks": fields[19]}
                )
            code = process.wait(timeout=timeout)
        except BaseException:
            kill_descendants(process.pid)
            try:
                os.killpg(process.pid, signal.SIGKILL) if new_session else process.kill()
            except ProcessLookupError:
                pass
            process.wait()
            raise
        finally:
            if new_session:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
    if code:
        raise RuntimeError(f"CLI stage exited {code}; see local log")


def command(arguments, log, timeout):
    run_process(
        [sys.executable, "-c", "from lexradar.cli import main; main()", *arguments],
        log,
        timeout,
        new_session=False,
    )


def process_site(url, directory, options, registry, normative_date):
    write_json(
        directory / "job.json",
        {
            "url": url,
            "options": options.model_dump(),
            "registry": str(registry) if registry else None,
            "normative_date": normative_date,
        },
    )
    try:
        run_process(
            [sys.executable, "-m", "lexradar.batch", str(directory)],
            directory / "worker.log",
            options.site_timeout,
        )
        return json.loads((directory / "site-result.json").read_text())
    except Exception as exc:
        return {
            "group": "technical_retry",
            "technical_status": "failed_or_partial",
            "reasons": [f"{type(exc).__name__}; inspect retained files"],
            "coverage": "Interrupted worker is not proof of absent documents",
        }


def rank(primary, policies, collection=None, identities=()):
    from .screening import signals

    found = signals(primary, policies, collection, identities)
    reasons = [s["reason"] for s in found]
    citations = [c for s in found for c in s["citations"]]
    if found:
        group, strength = "priority_review", max(s["evidence_strength"] for s in found)
    elif primary.get("forms") or policies:
        group, strength = "waiting", 20
        reasons = ["Form/document observed; no strong supported signal in bounded scope"]
    else:
        group, strength = "low_priority", 0
        reasons = ["No strong sign in examined scope; not a legally clean site"]
    return {
        "group": group,
        "evidence_strength": strength,
        "evidence_scale": "static support, not confirmed legal violation",
        "commercial_fit": 40 if found else 10 if primary.get("forms") else 0,
        "commercial_scale": "conditional documents/specification fit, not willingness to pay",
        "reasons": reasons,
        "citations": citations,
        "signals": found,
        "deferred_signals": ["Required-information absence: coverage/person/norm not established"],
        "paid_work_needed": False,
        "legal_status": "not_confirmed",
    }


def _process_site(url, directory, options, registry, normative_date):
    started = time.monotonic()
    stages = []
    try:
        stages_args = [
            (
                "collect",
                [
                    "collect",
                    url,
                    "--output",
                    str(directory / "dossier"),
                    "--max-pages",
                    str(options.max_pages),
                    "--max-documents",
                    str(options.max_documents),
                    "--max-requests",
                    str(options.max_requests),
                    "--max-duration",
                    str(options.crawl_duration),
                    "--min-delay",
                    str(options.min_delay),
                ],
            ),
            (
                "primary",
                [
                    "primary-audit",
                    str(directory / "dossier"),
                    "--output",
                    str(directory / "primary"),
                ],
            ),
            (
                "verify",
                [
                    "verify-primary",
                    str(directory / "dossier"),
                    "--primary",
                    str(directory / "primary/audit.json"),
                    "--assessment",
                    str(directory / "primary/internal-assessment.json"),
                    "--output",
                    str(directory / "verified"),
                ],
            ),
        ]
        if registry:
            stages_args.append(
                (
                    "normative",
                    [
                        "verify-norms",
                        str(directory / "dossier"),
                        "--primary",
                        str(directory / "primary/audit.json"),
                        "--assessment",
                        str(directory / "primary/internal-assessment.json"),
                        "--registry",
                        str(registry),
                        "--event-date",
                        normative_date,
                        "--output",
                        str(directory / "normative"),
                    ],
                )
            )
        normative_errors = []
        for stage, args in stages_args:
            remaining = options.site_timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise subprocess.TimeoutExpired(stage, options.site_timeout)
            try:
                command(args, directory / f"{stage}.log", remaining)
            except Exception as exc:
                if stage != "normative":
                    raise
                normative_errors.append(type(exc).__name__)
                continue
            stages.append(stage)
        collection_bytes = (directory / "dossier/collection.json").read_bytes()
        collection = json.loads(collection_bytes)
        collection["manifest_sha256"] = hashlib.sha256(collection_bytes).hexdigest()
        primary = json.loads((directory / "primary/audit.json").read_text())
        available = any(e["available"] for e in collection["evidence"])
        if not available:
            return {
                "group": "technical_retry",
                "reasons": ["No usable collection evidence; not absence"],
                "completed_stages": stages,
                "technical_status": "unavailable",
            }
        # Independent policy inventory also works without a supplied norm registry.
        from .verifier.offline import LocalResearch
        from .verifier.saved_policy import policy_observations

        research = LocalResearch(
            directory / "dossier",
            registry=None,
            sector="unknown",
            as_of=datetime.now(UTC),
            max_input_bytes=2_000_000,
        )
        policies = [p.model_dump(mode="json") for p in policy_observations(research)]
        from .screening import identity_statements

        result = rank(primary, policies, collection, identity_statements(research))
        result.update(
            completed_stages=stages,
            technical_status="partial",
            coverage="bounded saved material only; entire site not examined",
            normative_status="failed_with_gap"
            if normative_errors
            else "unverified_research_only"
            if registry
            else "not_requested",
            normative_errors=normative_errors,
        )
        write_json(directory / "screening.json", result)
        return result
    except Exception as exc:
        return {
            "group": "technical_retry",
            "reasons": [f"{type(exc).__name__}; inspect retained logs"],
            "completed_stages": stages,
            "technical_status": "failed_or_partial",
            "coverage": "No absence conclusion; existing files retained",
        }


def export_queue(root, state):
    rows = [
        dict(
            url=s["url"],
            **s.get(
                "result",
                {"group": "technical_retry", "reasons": ["Pending/interrupted; not yet evaluated"]},
            ),
            status=s["status"],
            attempt=s.get("attempt"),
            review_status="pending",
            production_outcome="HOLD",
            client_release_allowed=False,
            automatic_send_allowed=False,
        )
        for s in state["sites"]
    ]
    order = {"priority_review": 0, "waiting": 1, "low_priority": 2, "technical_retry": 3}
    rows.sort(
        key=lambda r: (
            order[r["group"]],
            -r.get("evidence_strength", 0),
            -r.get("commercial_fit", 0),
            r["url"],
        )
    )
    write_json(
        root / "review-queue.json",
        {
            "audience": "internal_only",
            "rows": rows,
            "production_outcome": "HOLD",
            "client_release_allowed": False,
            "automatic_send_allowed": False,
            "llm_budget_usd": 0,
        },
    )
    body = [
        '<!doctype html><meta charset="utf-8"><title>Internal review queue</title>',
        "<h1>Internal only — HOLD — no client release</h1><table>",
        "<tr><th>URL</th><th>Group</th><th>Evidence / commercial fit</th>"
        "<th>Reasons / files</th></tr>",
    ]
    for row in rows:
        link = ""
        if row["attempt"]:
            path = f"{row['attempt']}/primary/audit.json"
            if (root / path).is_file():
                link = f'<a href="{html.escape(path, quote=True)}">internal audit</a>'
        body.append(
            "<tr><td>"
            + html.escape(row["url"])
            + "</td><td>"
            + row["group"]
            + "</td><td>"
            + str(row.get("evidence_strength", 0))
            + " / "
            + str(row.get("commercial_fit", 0))
            + "</td><td>"
            + html.escape("; ".join(row["reasons"]))
            + " "
            + link
            + "</td></tr>"
        )
    safe_path(root / "review-queue.html").write_text("\n".join(body) + "</table>")


def engine_fingerprint():
    from importlib.metadata import version

    package = Path(__file__).parent
    files = {
        str(p.relative_to(package)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in package.rglob("*")
        if p.is_file() and p.suffix in {".py", ".txt"}
    }
    return digest(
        {
            "files": files,
            "python": sys.version,
            "dependencies": {name: version(name) for name in ("pydantic", "pypdf", "playwright")},
        }
    )


def registry_inputs(path):
    if path is None:
        return None
    from .verifier.legal_cards import read_local
    from .verifier.registry import load_registry

    try:
        registry, sha = load_registry(path)
    except (OSError, ValueError) as exc:
        # Invalid/missing normative data must not block technical screening.
        try:
            raw_fingerprint = hashlib.sha256(read_local(path)).hexdigest()
        except (OSError, ValueError):
            raw_fingerprint = "unavailable_or_oversized"
        return {"registry": raw_fingerprint, "normative_gap": type(exc).__name__}
    files = {card.original_path for card in registry.cards}
    files.update(m.path for c in registry.research_contexts for m in c.materials)
    result = {"registry": sha}
    for name in sorted(files):
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Unsafe registry material")
        material = safe_path(path.parent / relative)
        result[name] = (
            hashlib.sha256(read_local(material)).hexdigest() if material.is_file() else "missing"
        )
    return result


def run_batch(urls, output, options, *, registry=None, normative_date=None, resume=False):
    normalized, invalid = [], set()
    for url in urls:
        if len(url) > 2048:
            raise ValueError("URL length limit")
        try:
            normalized.append(canonical_url(url))
        except CollectionError:
            normalized.append(url)
            invalid.add(url)
    canonical = list(dict.fromkeys(normalized))
    if not canonical or len(canonical) > options.max_sites:
        raise ValueError("Empty or over-limit URL list")
    if bool(registry) != bool(normative_date):
        raise ValueError("Registry and explicit normative date must be supplied together")
    registry = safe_path(registry) if registry else None
    if normative_date:
        from datetime import date

        date.fromisoformat(normative_date)
    plan = {
        "urls": canonical,
        "options": options.model_dump(),
        "registry": str(registry) if registry else None,
        "registry_inputs": registry_inputs(registry),
        "normative_date": normative_date,
        "llm_budget_usd": 0,
        "engine_sha256": engine_fingerprint(),
    }
    root = safe_path(output)
    root.mkdir(parents=True, exist_ok=resume)
    lock = safe_path(root / ".lock")
    with lock.open("a") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Batch is already running") from exc
        path = safe_path(root / "batch.json")
        if resume:
            state = json.loads(path.read_text())
            if state["plan_sha256"] != digest(plan):
                raise ValueError("Resume plan changed; use a new output")
            if [s["url"] for s in state["sites"]] != canonical:
                raise ValueError("Resume site list changed")
            for site in state["sites"]:
                expected = hashlib.sha256(site["url"].encode()).hexdigest()[:20]
                if site["id"] != expected or not isinstance(site["attempts"], int):
                    raise ValueError("Invalid resume identity")
                if site.get("attempt") and not re.fullmatch(
                    rf"sites/{expected}/attempt-[0-9]{{4}}", site["attempt"]
                ):
                    raise ValueError("Unsafe resume attempt path")
                if site.get("attempt") and site["status"] != "done":
                    stop_previous_worker(safe_path(root / site["attempt"]))
                if (
                    site["status"] == "done"
                    and site.get("attempt")
                    and (
                        not (root / site["attempt"]).is_dir()
                        or snapshot(root / site["attempt"]) != site["files"]
                    )
                ):
                    site["status"] = "pending"
                    site["result"] = {
                        "group": "technical_retry",
                        "reasons": ["Saved files changed"],
                    }
        else:
            state = {
                "schema": "batch-screening-0.1",
                "plan": plan,
                "plan_sha256": digest(plan),
                "started_at": datetime.now(UTC).isoformat(),
                "sites": [
                    {
                        "url": u,
                        "id": hashlib.sha256(u.encode()).hexdigest()[:20],
                        "status": "pending",
                        "attempts": 0,
                    }
                    for u in canonical
                ],
            }
        for site in state["sites"]:
            if site["url"] in invalid:
                site["status"] = "done"
                site["result"] = {
                    "group": "technical_retry",
                    "technical_status": "invalid_input",
                    "reasons": ["Invalid URL; no collection attempted"],
                }
        write_json(path, state)
        export_queue(root, state)
        pending = [s for s in state["sites"] if s["status"] != "done"]
        active, hosts = {}, set()
        with concurrent.futures.ThreadPoolExecutor(max_workers=options.workers) as pool:
            while pending or active:
                for site in list(pending):
                    host = urlsplit(site["url"]).hostname
                    if len(active) >= options.workers or host in hosts:
                        continue
                    while True:
                        site["attempts"] += 1
                        site["attempt"] = f"sites/{site['id']}/attempt-{site['attempts']:04d}"
                        directory = safe_path(root / site["attempt"])
                        if not directory.exists():
                            break
                    directory.mkdir(parents=True, exist_ok=False)
                    site["status"] = "running"
                    write_json(path, state)
                    future = pool.submit(
                        process_site, site["url"], directory, options, registry, normative_date
                    )
                    active[future] = (site, host)
                    hosts.add(host)
                    pending.remove(site)
                completed, _ = concurrent.futures.wait(
                    active, return_when=concurrent.futures.FIRST_COMPLETED
                )
                for future in completed:
                    site, host = active.pop(future)
                    try:
                        site["result"] = future.result()
                        site["files"] = snapshot(root / site["attempt"])
                    except Exception as exc:
                        site["result"] = {
                            "group": "technical_retry",
                            "reasons": [
                                f"{type(exc).__name__}; unsafe/incomplete result, inspect locally"
                            ],
                        }
                        site["files"] = {}
                    site["status"] = (
                        "retry" if site["result"]["group"] == "technical_retry" else "done"
                    )
                    site["finished_at"] = datetime.now(UTC).isoformat()
                    hosts.remove(host)
                    write_json(path, state)
                    export_queue(root, state)
        return state


def batch_main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("urls", type=Path, help="UTF-8 file, one URL per line")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--normative-date")
    for name, field in BatchOptions.model_fields.items():
        parser.add_argument(
            "--" + name.replace("_", "-"),
            type=int
            if name in {"workers", "max_sites", "max_pages", "max_documents", "max_requests"}
            else float,
            default=field.default,
        )
    args = parser.parse_args(argv)
    try:
        options = BatchOptions(**{k: getattr(args, k) for k in BatchOptions.model_fields})
        urls = args.urls.read_text().splitlines()
        state = run_batch(
            [u.strip() for u in urls if u.strip()],
            args.output,
            options,
            registry=args.registry,
            normative_date=args.normative_date,
            resume=args.resume,
        )
    except (ValueError, OSError, CollectionError) as exc:
        parser.error(f"Batch failed safely ({type(exc).__name__}); no legal/client release")
    print(f"Sites: {len(state['sites'])}; review-queue.html; HOLD; LLM spend $0")


if __name__ == "__main__":
    directory = safe_path(Path(sys.argv[1]))
    job = json.loads((directory / "job.json").read_text())
    result = _process_site(
        job["url"],
        directory,
        BatchOptions(**job["options"]),
        Path(job["registry"]) if job["registry"] else None,
        job["normative_date"],
    )
    write_json(directory / "site-result.json", result)
