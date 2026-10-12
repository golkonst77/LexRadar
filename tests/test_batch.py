"""Synthetic queue/worker tests, never real websites or LLMs."""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from lexradar import batch


def fake_worker(monkeypatch, calls, *, fail=None):
    def run(url, directory, *args):
        calls.append(url)
        (directory / "artifact.txt").write_text(url)
        return {
            "group": "technical_retry" if url == fail else "waiting",
            "reasons": ["synthetic"],
            "evidence_strength": 0,
            "commercial_fit": 99,
        }

    monkeypatch.setattr(batch, "process_site", run)


def policy(domain="other.example", complete=True):
    return {
        "source_url": "https://clinic.example/",
        "text": f"Политика конфиденциальности интернет-сайта {domain}",
        "reading_completeness": "complete" if complete else "partial",
        "evidence_id": "p1",
        "artifact_path": "page.html",
        "artifact_sha256": "a" * 64,
        "block": {"status": "complete" if complete else "partial"},
    }


@pytest.mark.parametrize(
    "complete,domain,expected",
    [
        (True, "other.example", "priority_review"),
        (False, "other.example", "waiting"),
        (True, "clinic.example", "waiting"),
    ],
)
def test_priority_needs_complete_specific_document_mismatch(complete, domain, expected):
    result = batch.rank({}, [policy(domain, complete)])
    assert result["group"] == expected
    assert not result["paid_work_needed"] and result["legal_status"] == "not_confirmed"
    assert bool(result["citations"]) == (expected == "priority_review")


def test_no_checkbox_gtm_link_or_organization_count_ranking():
    result = batch.rank(
        {"forms": [], "organizations": ["a", "b"], "integration_groups": ["GTM", "booking"]}, []
    )
    assert result["group"] == "low_priority"
    assert result["evidence_strength"] == result["commercial_fit"] == 0
    assert "not a legally clean" in result["reasons"][0]


def test_form_only_waits():
    assert batch.rank({"forms": [{"checkbox": False}]}, [])["group"] == "waiting"


def test_dedup_resume_and_mutated_artifact(monkeypatch, tmp_path):
    calls = []
    fake_worker(monkeypatch, calls)
    urls = ["https://example.org", "https://example.org/#x", "https://two.org"]
    root = tmp_path / "run"
    opts = batch.BatchOptions()
    state = batch.run_batch(urls, root, opts)
    assert len(calls) == 2
    batch.run_batch(urls, root, opts, resume=True)
    assert len(calls) == 2
    old = root / state["sites"][0]["attempt"] / "artifact.txt"
    old.write_text("tampered")
    state = batch.run_batch(urls, root, opts, resume=True)
    assert len(calls) == 3 and state["sites"][0]["attempts"] == 2
    assert old.read_text() == "tampered"  # Never erase former attempt.
    queue = json.loads((root / "review-queue.json").read_text())
    assert queue["llm_budget_usd"] == 0 and queue["production_outcome"] == "HOLD"
    assert all(
        not r["client_release_allowed"] and not r["automatic_send_allowed"] for r in queue["rows"]
    )


def test_failed_site_does_not_block_others_or_get_low_priority(monkeypatch, tmp_path):
    calls = []
    fake_worker(monkeypatch, calls, fail="https://bad.org/")
    state = batch.run_batch(
        ["https://bad.org", "https://good.org"], tmp_path / "run", batch.BatchOptions()
    )
    assert [s["result"]["group"] for s in state["sites"]] == ["technical_retry", "waiting"]
    state = batch.run_batch(
        ["https://bad.org", "https://good.org"], tmp_path / "run", batch.BatchOptions(), resume=True
    )
    assert len(calls) == 3 and state["sites"][1]["attempts"] == 1


def test_worker_exception_isolated(monkeypatch, tmp_path):
    def worker(url, directory, *args):
        if "bad" in url:
            raise RuntimeError("synthetic")
        return {"group": "low_priority", "reasons": ["bounded"]}

    monkeypatch.setattr(batch, "process_site", worker)
    state = batch.run_batch(
        ["https://bad.org", "https://good.org"], tmp_path / "run", batch.BatchOptions()
    )
    assert [s["status"] for s in state["sites"]] == ["retry", "done"]


def test_per_host_serial_global_bound(monkeypatch, tmp_path):
    active = set()
    peak = 0
    lock = threading.Lock()

    def worker(url, directory, *args):
        nonlocal peak
        host = batch.urlsplit(url).hostname
        with lock:
            assert host not in active
            active.add(host)
            peak = max(peak, len(active))
        time.sleep(0.015)
        with lock:
            active.remove(host)
        return {"group": "waiting", "reasons": ["synthetic"]}

    monkeypatch.setattr(batch, "process_site", worker)
    batch.run_batch(
        ["https://a.org/1", "http://a.org/2", "https://b.org", "https://c.org"],
        tmp_path / "run",
        batch.BatchOptions(workers=2),
    )
    assert peak == 2


def test_interrupted_attempt_not_overwritten(monkeypatch, tmp_path):
    calls = []
    fake_worker(monkeypatch, calls)
    root = tmp_path / "run"
    urls = ["https://a.org"]
    state = batch.run_batch(urls, root, batch.BatchOptions())
    state["sites"][0]["status"] = "running"
    batch.write_json(root / "batch.json", state)
    newer = batch.run_batch(urls, root, batch.BatchOptions(), resume=True)
    assert newer["sites"][0]["attempts"] == 2
    assert (root / state["sites"][0]["attempt"]).is_dir()


def test_changed_plan_rejected(monkeypatch, tmp_path):
    calls = []
    fake_worker(monkeypatch, calls)
    root = tmp_path / "run"
    batch.run_batch(["https://a.org"], root, batch.BatchOptions())
    with pytest.raises(ValueError, match="plan changed"):
        batch.run_batch(["https://a.org"], root, batch.BatchOptions(workers=2), resume=True)


def test_resume_escape_rejected(monkeypatch, tmp_path):
    calls = []
    fake_worker(monkeypatch, calls)
    root = tmp_path / "run"
    urls = ["https://a.org"]
    state = batch.run_batch(urls, root, batch.BatchOptions())
    state["sites"][0]["attempt"] = "../../outside"
    batch.write_json(root / "batch.json", state)
    with pytest.raises(ValueError, match="Unsafe resume"):
        batch.run_batch(urls, root, batch.BatchOptions(), resume=True)


def test_symlink_rejected(tmp_path):
    (tmp_path / "outside").mkdir()
    (tmp_path / "run").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="Symlink"):
        batch.run_batch(["https://a.org"], tmp_path / "run", batch.BatchOptions(), resume=True)


def test_registry_needs_explicit_date(tmp_path):
    with pytest.raises(ValueError, match="explicit normative date"):
        batch.run_batch(
            ["https://a.org"], tmp_path / "run", batch.BatchOptions(), registry=tmp_path / "r"
        )


def test_budget_validation():
    with pytest.raises(ValueError):
        batch.BatchOptions(workers=5)
    with pytest.raises(ValueError):
        batch.BatchOptions(min_delay=1)
    with pytest.raises(ValueError):
        batch.BatchOptions(site_timeout=float("inf"))


def test_process_deadline(tmp_path):
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        batch.run_process(
            [sys.executable, "-c", "import time; time.sleep(10)"], tmp_path / "log", 0.1
        )
    assert time.monotonic() - start < 2


def test_html_escapes_user_values(tmp_path):
    batch.export_queue(
        tmp_path,
        {
            "sites": [
                {
                    "url": "https://a.org/<script>",
                    "status": "done",
                    "result": {"group": "waiting", "reasons": ["<script>attack</script>"]},
                }
            ]
        },
    )
    assert "<script>" not in (tmp_path / "review-queue.html").read_text()


def test_offline_stages_real_cli(monkeypatch, tmp_path):
    import shutil

    original = batch.command

    def command(args, log, timeout):
        if args[0] == "collect":
            output = Path(args[args.index("--output") + 1])
            shutil.copytree(Path("examples/primary-audit/dossier"), output)
            log.write_text("Synthetic local fixture; not network collection")
        else:
            original(args, log, timeout)

    monkeypatch.setattr(batch, "command", command)
    result = batch._process_site("https://example.org/", tmp_path, batch.BatchOptions(), None, None)
    assert result["group"] == "waiting"
    assert result["completed_stages"] == ["collect", "primary", "verify"]
    assert (tmp_path / "primary/audit.json").exists()
    assert json.loads((tmp_path / "primary/audit.json").read_text())["production_outcome"] == "HOLD"


def test_invalid_url_does_not_abort_batch(monkeypatch, tmp_path):
    calls = []
    fake_worker(monkeypatch, calls)
    state = batch.run_batch(
        ["ftp://wrong.org", "https://good.org"], tmp_path / "run", batch.BatchOptions()
    )
    assert calls == ["https://good.org/"]
    assert state["sites"][0]["result"]["group"] == "technical_retry"


@pytest.mark.parametrize("detached", [False, True])
def test_whole_worker_timeout_kills_descendant(tmp_path, detached):
    import os

    pidfile = tmp_path / "child.pid"
    code = (
        "import subprocess,time,pathlib; "
        'p=subprocess.Popen(["sleep","30"], start_new_session=DETACHED); '
        f"pathlib.Path({str(pidfile)!r}).write_text(str(p.pid)); time.sleep(30)"
    )
    code = code.replace("DETACHED", str(detached))
    with pytest.raises(subprocess.TimeoutExpired):
        batch.run_process([sys.executable, "-c", code], tmp_path / "log", 0.3)
    pid = int(pidfile.read_text())
    for _ in range(30):
        path = Path(f"/proc/{pid}/stat")
        if not path.exists() or path.read_text().split()[2] == "Z":
            break
        time.sleep(0.01)
    else:
        os.kill(pid, 9)
        pytest.fail("Descendant survived worker deadline")


def test_exclusive_batch_lock(monkeypatch, tmp_path):
    import fcntl

    calls = []
    fake_worker(monkeypatch, calls)
    root = tmp_path / "run"
    batch.run_batch(["https://a.org"], root, batch.BatchOptions())
    with (root / ".lock").open("a") as file:
        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="already running"):
            batch.run_batch(["https://a.org"], root, batch.BatchOptions(), resume=True)


def test_registry_material_fingerprint_changes(tmp_path):
    import shutil

    shutil.copytree("examples/legal-sources", tmp_path / "sources")
    path = tmp_path / "sources/registry.json"
    before = batch.registry_inputs(path)
    original = tmp_path / "sources/originals/v1.txt"
    original.write_text("changed")
    assert batch.registry_inputs(path) != before


def test_resume_stops_identified_orphan_worker(tmp_path):
    import os

    package = tmp_path / "lexradar"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "batch.py").write_text("import time; time.sleep(30)")
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    process = subprocess.Popen(
        [sys.executable, "-m", "lexradar.batch", str(attempt)],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        start_new_session=True,
    )
    try:
        fields = Path(f"/proc/{process.pid}/stat").read_text().rsplit(")", 1)[1].split()
        batch.write_json(
            attempt / "worker.pid.json", {"pid": process.pid, "start_ticks": fields[19]}
        )
        batch.stop_previous_worker(attempt)
        assert process.wait(timeout=2) == -9
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


def test_saved_pid_alone_cannot_kill_unrelated_process(tmp_path):
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        fields = Path(f"/proc/{process.pid}/stat").read_text().rsplit(")", 1)[1].split()
        batch.write_json(
            tmp_path / "worker.pid.json", {"pid": process.pid, "start_ticks": fields[19]}
        )
        batch.stop_previous_worker(tmp_path)
        assert process.poll() is None
    finally:
        process.kill()
        process.wait()


def test_engine_change_requires_new_batch(monkeypatch, tmp_path):
    calls = []
    fake_worker(monkeypatch, calls)
    urls = ["https://a.org"]
    root = tmp_path / "run"
    batch.run_batch(urls, root, batch.BatchOptions())
    monkeypatch.setattr(batch, "engine_fingerprint", lambda: "different-code")
    with pytest.raises(ValueError, match="plan changed"):
        batch.run_batch(urls, root, batch.BatchOptions(), resume=True)


def test_resume_preserves_uncommitted_attempt_directory(monkeypatch, tmp_path):
    calls = []
    fake_worker(monkeypatch, calls)
    root = tmp_path / "run"
    state = batch.run_batch(["https://a.org"], root, batch.BatchOptions())
    site = state["sites"][0]
    site["status"] = "running"
    orphan = root / "sites" / site["id"] / "attempt-0002"
    orphan.mkdir()
    (orphan / "retained.txt").write_text("Interrupted before state update")
    batch.write_json(root / "batch.json", state)
    state = batch.run_batch(["https://a.org"], root, batch.BatchOptions(), resume=True)
    assert state["sites"][0]["attempts"] == 3
    assert (orphan / "retained.txt").read_text() == "Interrupted before state update"


def test_normative_failure_does_not_discard_completed_screening(monkeypatch, tmp_path):
    import shutil

    original = batch.command

    def command(args, log, timeout):
        if args[0] == "collect":
            shutil.copytree(
                "examples/primary-audit/dossier", Path(args[args.index("--output") + 1])
            )
            log.write_text("Synthetic fixture")
        elif args[0] == "verify-norms":
            raise RuntimeError("Synthetic unavailable norm stage")
        else:
            original(args, log, timeout)

    monkeypatch.setattr(batch, "command", command)
    result = batch._process_site(
        "https://example.org/",
        tmp_path,
        batch.BatchOptions(),
        Path("examples/legal-sources/registry.json"),
        "2026-10-11",
    )
    assert result["group"] != "technical_retry"
    assert result["normative_status"] == "failed_with_gap"
    assert "primary" in result["completed_stages"]
