"""Fresh versioned verifier requests using the existing bounded OpenRouter transport."""

import json
import time
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

from ..auditors.budget import Budget, BudgetExceeded
from ..auditors.models import Usage
from ..auditors.preflight import authorize
from ..auditors.provider import ProviderError, ProviderReply
from .models import ComparisonResult, IndependentResult, VerifierConfig, VerifierRequestLog
from .registry import digest


def messages_for(stage, packet):
    version = f"{stage}-v1"
    system = (
        files("lexradar.verifier").joinpath(f"prompts/{version}.txt").read_text(encoding="utf-8")
    )
    schema = IndependentResult if stage == "independent" else ComparisonResult
    system += "\nOutput JSON schema:\n" + json.dumps(schema.model_json_schema(), ensure_ascii=False)
    return (
        [{"role": "system", "content": system}, {"role": "user", "content": packet.payload}],
        version,
        digest(system),
    )


class OfflineVerifierProvider:
    def complete(self, settings, messages):
        packet = json.loads(messages[1]["content"])
        if packet["stage"] == "independent":
            from ..auditors.models import REQUIRED_TOPICS

            result = IndependentResult(
                materials=[
                    {
                        "evidence_id": m["evidence_id"],
                        "text_examined": False,
                        "pages_examined": [],
                        "limitation": "Offline placeholder; no legal examination",
                    }
                    for m in packet["materials"]
                ],
                directions=[
                    {
                        "topic": topic,
                        "status": "insufficient_evidence",
                        "limitation": "Offline placeholder; no LLM",
                    }
                    for topic in sorted(REQUIRED_TOPICS)
                ],
                limitations=["Offline demonstration only; no legal search performed"],
            )
        else:
            result = ComparisonResult(
                assessments=[
                    {
                        "auditor": r["auditor"],
                        "finding_id": f["id"],
                        "status": "insufficient_evidence",
                        "reason": "Offline placeholder; no model comparison",
                        "matched_independent_ids": [],
                    }
                    for r in packet["untrusted_auditor_results"]
                    for f in r["findings"]
                ],
                limitations=["Offline demonstration only"],
            )
        return ProviderReply(result.model_dump_json(), Usage(cost_usd=0))


def request(
    stage: str,
    packet,
    config: VerifierConfig,
    provider,
    budget: Budget,
    mode: str,
    approval_path: Path | None,
    sleep=time.sleep,
):
    messages, version, prompt_hash = messages_for(stage, packet)
    logs = []
    for attempt in range(config.retries + 1):
        started, timer = datetime.now(UTC), time.monotonic()
        sent, retry, charge = False, False, 0
        usage = Usage()
        result = None
        status = "provider_error"
        try:
            if mode == "openrouter":
                authorize(packet, approval_path)
                charge = budget.reserve(config.verifier, messages)
            sent = mode == "openrouter"
            reply = provider.complete(config.verifier, [dict(m) for m in messages])
            usage = reply.usage
            if mode == "openrouter":
                charge = budget.settle(charge, usage)
                if budget.overrun:
                    raise BudgetExceeded()
            result = (
                IndependentResult if stage == "independent" else ComparisonResult
            ).model_validate_json(reply.content)
            status = "success"
        except BudgetExceeded:
            status = "budget_exceeded"
        except ProviderError as exc:
            status = exc.code
            retry = exc.retryable and attempt < config.retries
        except ValueError:
            status = "invalid_response"
        logs.append(
            VerifierRequestLog(
                stage=stage,
                model=config.verifier.model,
                started_at=started,
                duration_seconds=max(0, time.monotonic() - timer),
                prompt_version=version,
                prompt_sha256=prompt_hash,
                packet_sha256=packet.sha256,
                attempt=attempt + 1,
                request_sent=sent,
                status=status,
                usage=usage,
                budget_charge_usd=charge,
            )
        )
        if not retry:
            return result, logs, status
        sleep(min(config.max_backoff_seconds, 2**attempt))
    raise AssertionError("Bounded request loop must return")
