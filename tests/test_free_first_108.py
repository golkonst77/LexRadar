"""Scope-bounded free checks, all five execute, no provider can be reached."""

import json
import socket

import pytest

from examples.free_first_108 import POLICY, SEPARATE, make_dossier
from lexradar.auditors.evidence import DossierError
from lexradar.free_first import confirmed_findings, inspect, main, run


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("External call prohibited")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr("lexradar.auditors.provider.OpenRouterTransport.post", forbidden)


def rows(tmp_path, **kwargs):
    _, checks, _ = inspect(make_dossier(tmp_path / "dossier", **kwargs))
    assert [r["check_id"] for r in checks] == [f"WEB-0{i}" for i in range(1, 6)]
    return {r["check_id"]: r for r in checks}


def test_policy_explicit_same_scope_conflict_is_fail(tmp_path):
    row = rows(tmp_path)["WEB-01"]
    assert row["status"] == "FAIL" and row["materiality"] == "substantial"
    assert row["findings"][0]["evidence"][0]["exact_quote"] in POLICY
    assert row["policy3"][0]["status"] == "REVIEW"  # retain original POLICY-3 judgments


@pytest.mark.parametrize(
    "policy",
    [
        POLICY.replace("90", "30"),
        "<p>Цели обработки — обработка заявок.</p>",
        "<p>Срок хранения заявок — 30 дней. Срок хранения договоров — 90 дней.</p>",
        "<p>Срок хранения заявок — 30 дней при отказе; 90 дней после договора.</p>",
    ],
)
def test_policy_missing_keywords_or_different_scope_is_not_fail(tmp_path, policy):
    assert rows(tmp_path, policy=policy)["WEB-01"]["status"] == "REVIEW"


def test_script_and_banner_are_not_dynamic_consent_failure(tmp_path):
    row = rows(tmp_path, analytics=True, extra="<p>Cookie используются на сайте.</p>")["WEB-02"]
    assert row["status"] == "REVIEW"
    assert row["static_inventory"]["pages"][0]["static_service_hints"] == ["yandex_metrika"]
    assert row["static_inventory"]["dynamic_status"] == "NOT_VERIFIED"


def test_no_analytics_does_not_prove_absence(tmp_path):
    assert rows(tmp_path)["WEB-02"]["status"] == "REVIEW"


@pytest.mark.parametrize("extra", ["", "<p>Нет сведений об основании обработки.</p>"])
def test_forms_preserve_prechecked_field_but_do_not_invent_legal_basis(tmp_path, extra):
    row = rows(tmp_path, extra=extra)["WEB-03"]
    assert row["status"] == "REVIEW"
    assert row["evidence"][0]["fields"][2]["checked"] is True
    assert row["evidence"][0]["submission_not_attempted"]


def test_no_forms_remains_review(tmp_path):
    result = rows(tmp_path, form=False)
    assert result["WEB-03"]["status"] == "REVIEW"
    assert result["WEB-05"]["status"] == "REVIEW"


def test_explicit_embedded_consent_separate_from_checkbox_check(tmp_path):
    result = rows(tmp_path, extra=SEPARATE)
    assert result["WEB-04"]["status"] == "FAIL"
    assert result["WEB-03"]["status"] == "REVIEW"
    assert result["WEB-04"]["findings"][0]["evidence"][0]["start"] >= 0


def test_regular_appointment_is_not_advertising(tmp_path):
    assert rows(tmp_path)["WEB-05"]["status"] == "NOT_APPLICABLE"


def test_advertising_without_delivery_evidence_is_review(tmp_path):
    row = rows(tmp_path, extra="<p>Подписка на рекламную рассылку.</p>")["WEB-05"]
    assert row["status"] == "REVIEW" and row["evidence"]


def test_one_fail_go_all_checks_no_ai_and_no_sending(tmp_path):
    result = run(make_dossier(tmp_path / "dossier"), tmp_path / "out")
    assert result["outcome"] == "GO" and len(result["checks"]) == 5
    assert result["new_ai_calls"] == 0 and result["new_ai_cost_usd"] == "0"
    assert result["manual_approval_required"] and not result["ai_approved"]
    assert not any(result[k] for k in ["client_release_allowed", "automatic_send_allowed", "sent"])
    page = (tmp_path / "out/proposal.html").read_text()
    assert "45 000 ₽" in page and "33 750 ₽" in page and "2 месяца" in page
    assert "Ч. 16 — 10–15 млн ₽" in page and "v2.4" in page


def test_many_fails_one_proposal_and_deduplication(tmp_path):
    root = make_dossier(tmp_path / "dossier", extra=SEPARATE)
    result = run(root, tmp_path / "out")
    assert len(result["findings"]) == 2
    assert len(confirmed_findings(result["checks"] * 2)) == 2
    assert len(list((tmp_path / "out").glob("proposal*"))) == 1


def test_only_reviews_pending_separate_approval_no_proposal(tmp_path):
    result = run(make_dossier(tmp_path / "dossier", policy="<p>Политика.</p>"), tmp_path / "out")
    assert result["outcome"] == "AI_PENDING_APPROVAL" and result["route"] == "AI_CANDIDATE"
    assert not (tmp_path / "out/proposal.html").exists()


def test_empty_materials_hold(tmp_path):
    root = make_dossier(tmp_path / "dossier", policy="", form=False)
    # Remove all substantive text without fabricating unavailable evidence.
    data = json.loads((root / "collection.json").read_text())
    data.update(pages=[], evidence=[])
    (root / "collection.json").write_text(json.dumps(data))
    assert run(root, tmp_path / "out")["outcome"] == "HOLD"


def test_integrity_error_never_creates_go(tmp_path):
    root = make_dossier(tmp_path / "dossier")
    (root / "artifacts/page-0001.html").write_text("tampered")
    with pytest.raises(DossierError):
        run(root, tmp_path / "out")
    assert not (tmp_path / "out/proposal.html").exists()


def test_cli_offline_and_existing_output_protected(tmp_path, capsys):
    root = make_dossier(tmp_path / "dossier")
    main(["--dossier", str(root), "--output", str(tmp_path / "out")])
    assert "FREE_GO" in capsys.readouterr().out
    with pytest.raises(ValueError, match="new directory"):
        run(root, tmp_path / "out")


def test_quoted_example_is_not_an_operator_defect(tmp_path):
    assert (
        rows(tmp_path, policy="<p>Пример ошибочной формулировки:</p>" + POLICY)["WEB-01"]["status"]
        == "REVIEW"
    )


def test_url_mode_reuses_bounded_collector(tmp_path, monkeypatch):
    calls = []

    def collect(url, output, limits, **kwargs):
        assert kwargs["static_only"] and url == "https://business.example.invalid/"
        calls.append(url)
        make_dossier(output)

    monkeypatch.setattr("lexradar.free_first.collect", collect)
    main(["https://business.example.invalid/", "--output", str(tmp_path / "out")])
    assert len(calls) == 1
