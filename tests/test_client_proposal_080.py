import copy
import json
from pathlib import Path

import pytest

from lexradar.client_proposal import pricing, render_client
from lexradar.proposal_export import build
from lexradar.proposal_rules import NoProposalTriggers

SOURCE = Path("runs-076/result.json")
COLLECTION = Path("runs-054/doktstom-improved/dossier/collection.json")
CONFIG = json.loads(Path("scripts/proposal-080-config.json").read_text())


def test_export_structure_identity_and_gates(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Network used")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    state = build(SOURCE, COLLECTION, CONFIG, tmp_path / "export")
    page = (tmp_path / "export/proposal-doktstom.html").read_text()
    assert (
        page.split('<ol class="findings editable" contenteditable="true">')[1]
        .split("</ol>")[0]
        .count("<li>")
        == 4
    )
    assert page.index("техническое обслуживание") < page.index("администрация Сайта")
    for heading in (
        "КОММЕРЧЕСКОЕ ПРЕДЛОЖЕНИЕ",
        "ЧТО ВЫЯВЛЕНО",
        "ЧТО ЭТО ЗНАЧИТ ДЛЯ БИЗНЕСА",
        "ЧТО ВХОДИТ В КОМПЛЕКС РАБОТ",
        "СТОИМОСТЬ И СРОКИ",
        "ПОЧЕМУ НЕЛЬЗЯ ПРОСТО ОТДАТЬ ВЕБМАСТЕРУ",
        "СЛЕДУЮЩИЕ ШАГИ",
        "Доктор СТОМ",
        "Просто",
    ):
        assert heading in page
    for secret in (
        "REVIEW_CANDIDATE",
        "HOLD",
        "GO",
        "page-0001",
        "evidence_id",
        "runs-",
        "SHA",
        "DeepSeek",
        "<script",
        "<form",
    ):
        assert secret not in page
    assert state["manual_approval_required"] and state["manual_approval"] is None
    assert not any(
        state[k] for k in ("automatic_send_allowed", "client_release_allowed", "legal_go", "sent")
    )
    internal = json.loads((tmp_path / "export/internal-report.json").read_text())
    assert internal["audit"] == json.loads(SOURCE.read_text())


@pytest.mark.parametrize("failure", ["quote", "identity", "url"])
def test_reject_invented_facts(tmp_path, failure):
    audit = json.loads(SOURCE.read_text())
    config = copy.deepcopy(CONFIG)
    if failure == "quote":
        audit["candidates"][0]["grounding"]["exact_quote"] = "Выдуманное нарушение"
    elif failure == "identity":
        config["client"]["name"] = "АСТРО"
    else:
        config["client"]["url"] = "https://other.test/"
    source = tmp_path / "source.json"
    source.write_text(json.dumps(audit))
    with pytest.raises(ValueError):
        build(source, COLLECTION, config, tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_no_confirmed_observations_no_accusations():
    with pytest.raises(NoProposalTriggers):
        render_client(
            CONFIG["client"], [dict(confirmed=False, quote="invented")], CONFIG["commercial"]
        )


def test_tariff_and_explicit_discount():
    config = dict(
        tariffs={"base": dict(price=25000, term="10 рабочих дней")},
        selected_tariff="base",
        existing_client_discount_percent=20,
    )
    assert pricing(config) == ("Стоимость: 25 000.00 ₽", "Срок: 10 рабочих дней")
    config["apply_existing_client_discount"] = True
    with pytest.raises(ValueError):
        pricing(config)
    config["existing_client_confirmed"] = True
    assert "20 000.00" in pricing(config)[0]
    assert "согласуем" in pricing({})[0]


def test_no_seller_substitution():
    with pytest.raises(ValueError):
        render_client(CONFIG["client"], [], dict(sender="ООО ПростоБюро"))


def test_untrusted_fragments_cannot_bypass_trigger_gate():
    with pytest.raises(NoProposalTriggers):
        render_client(
            dict(name="<script>name</script>", url="https://example.test/"),
            [dict(confirmed=True, title="<script>x</script>", quote="<img>", meaning="<&>")],
            dict(date="16.10.2026"),
        )
