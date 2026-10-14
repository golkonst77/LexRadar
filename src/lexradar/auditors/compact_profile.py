"""Fail-closed pilot profile verified against public OpenRouter metadata."""

from datetime import UTC, datetime
from decimal import Decimal

MODEL = "deepseek/deepseek-v3.2"
PROVIDER = "deepinfra/fp4"
INPUT_PRICE = Decimal("0.26")
OUTPUT_PRICE = Decimal("0.38")
VERIFIED_UNTIL = datetime(2026, 10, 15, tzinfo=UTC)


def validate_profile(config):
    settings = config.auditor_a
    if datetime.now(UTC) >= VERIFIED_UNTIL:
        raise PermissionError("Model metadata expired; public-source revalidation required")
    if (
        settings.model != MODEL
        or settings.prompt_price_cap != INPUT_PRICE
        or settings.completion_price_cap != OUTPUT_PRICE
        or settings.max_output_tokens > 16384
        or config.retries != 0
        or not config.single_auditor
    ):
        raise PermissionError("Unverified paid profile; no fallback permitted")
