"""One explicitly confirmed paid control audit using the task 100 packet."""

import json
import os
import shutil
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from lexradar.audit_review import export
from lexradar.auditors.budget import Budget
from lexradar.auditors.evidence import load_packet
from lexradar.auditors.orchestrator import analyze
from lexradar.auditors.preflight import PacketApproval, screen
from lexradar.single_auditor import (
    DeepSeekAdapter, commercial, messages_for, profile,
)

os.umask(0o077)
source = Path("runs-100/compact")
output = Path("runs-101")
expected = "db4af395dee973704e3ade8cc7691b382c7ca23e5502754c57b677ee36fa7205"

if output.exists():
    raise SystemExit("Отмена: runs-101 уже существует.")

config = profile(model="openai/gpt-5-mini", timeout_seconds=120)
assert config.single_auditor and config.retries == 0
assert config.max_budget_usd == Decimal("0.50")
assert config.auditor_a.max_output_tokens == 28000

packet = load_packet(source, 200000)
assert packet.sha256 == expected, "Пакет №100 изменился"
assert packet.payload.encode() == Path("runs-100/review/packet.json").read_bytes()
messages, version, prompt_hash = messages_for("A", packet)
assert version == "single-a-v3-cookie"
assert prompt_hash == "939ce61428091ff22a51a9bd92f5ba2a167b86b0451f47ddf4eb1120a2872790"
assert "cookie_analytics_review" in messages[0]["content"]
assert sum(len(m["content"].encode()) for m in messages) + 1024 <= 240000
reserve = Budget(config.max_budget_usd).reserve(config.auditor_a, messages)

confirmation = (
    "ПАКЕТ ПРОСМОТРЕН. РАЗРЕШАЮ передачу без скрининга ПДн, "
    "одну попытку GPT-5 Mini и бюджет 0.50 USD"
)
with open("/dev/tty", "r") as tty, open("/dev/tty", "w") as tty_out:
    tty_out.write(
        f"Пакет: runs-100/review/packet.json\n"
        f"SHA-256: {packet.sha256}\n"
        f"Размер: {len(packet.payload.encode())} байт\n"
        f"Резерв: ${reserve}; бюджет: $0.50\n"
        f"Срабатываний скрининга: {len(screen(packet))}\n"
        "Просмотрите пакет и runs-100/review/preflight.json.\n"
        "Скрининг ПДн будет отключён; проверки доказательств сохраняются.\n"
        "Введите имя согласующего: "
    )
    tty_out.flush()
    reviewer = tty.readline().strip()
    if not reviewer or len(reviewer) > 200:
        raise SystemExit("Отмена: не указан согласующий.")
    tty_out.write(f"Для запуска введите точно:\n{confirmation}\n")
    tty_out.flush()
    if tty.readline().strip() != confirmation:
        raise SystemExit("Запуск отменён.")

key = os.environ.get("OPENROUTER_API_KEY")
if not key:
    key = Path("/home/golkonst/.config/lexradar/openrouter.key").read_text().strip()
os.environ["OPENROUTER_API_KEY"] = key
provider = DeepSeekAdapter(model="openai/gpt-5-mini", timeout_seconds=120)
del key

output.mkdir(mode=0o700, exist_ok=False)
shutil.copytree(source, output / "dossier")
for name in ("quotes.json", "selection.json", "source-inventory.json", "validation.json"):
    shutil.copyfile(Path("runs-100") / name, output / name)

snapshot = load_packet(output / "dossier", 200000)
assert snapshot.sha256 == expected
approval = PacketApproval(
    packet_sha256=expected,
    approved=True,
    contents_reviewed=True,
    reviewer=reviewer,
    approved_at=datetime.now(UTC),
)
approval_path = output / "approval.json"
approval_path.write_text(approval.model_dump_json(indent=2), encoding="utf-8")

started = time.monotonic()
try:
    report = analyze(
        output / "dossier",
        output / "audit",
        config,
        mode="openrouter",
        allow_external_transfer=True,
        packet_approval=approval_path,
        provider=provider,
        message_builder=messages_for,
        mvp_no_screening=True,
    )
except Exception as exc:
    (output / "execution-error.json").write_text(json.dumps({
        "error_type": type(exc).__name__,
        "elapsed_seconds": time.monotonic() - started,
        "retry_permitted": False,
        "provider_cost_unknown": True,
    }, indent=2), encoding="utf-8")
    raise SystemExit(
        "Запуск завершился ошибкой. Автоматического повтора нет. "
        "См. runs-101; перед любым повтором проверьте состояние запроса."
    )

allowed, reasons = commercial(report, snapshot)
summary = {
    "completed": report.completed,
    "elapsed_seconds": time.monotonic() - started,
    "packet_sha256": expected,
    "prompt": version,
    "diagnostic_outcome": "HOLD" if reasons else "GO",
    "reasons": reasons,
    "allowed_findings": allowed,
    "usage": [log.usage.model_dump(mode="json") for log in report.logs],
    "budget_charged_usd": str(report.budget_charged_usd),
    "budget_limit_usd": str(report.budget_limit_usd),
    "cost_note": "При неизвестной стоимости учитывается резерв.",
    "automatic_send_allowed": False,
}
(output / "summary.json").write_text(
    json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
)

export(output / "audit", output / "review", dossier=output / "dossier")
print("Готово: runs-101/summary.json и runs-101/review/")
