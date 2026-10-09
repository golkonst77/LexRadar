"""Reserve conservative token-price bounds before each billable attempt."""

from decimal import Decimal

from .models import ModelSettings, Usage


class BudgetExceeded(Exception):
    pass


class Budget:
    def __init__(self, limit: Decimal):
        self.limit = limit
        self.charged = Decimal(0)
        self.overrun = False

    def reserve(self, settings: ModelSettings, messages: list[dict[str, str]]) -> Decimal:
        # UTF-8 bytes upper-bound ordinary text token counts; overhead includes chat framing.
        input_bound = sum(len(m["content"].encode()) for m in messages) + 1024
        estimate = (
            Decimal(input_bound) * settings.prompt_price_cap
            + Decimal(settings.max_output_tokens) * settings.completion_price_cap
        ) / 1_000_000
        if self.overrun or self.charged + estimate > self.limit:
            raise BudgetExceeded("Insufficient audit budget for another attempt")
        self.charged += estimate
        return estimate

    def settle(self, reserved: Decimal, usage: Usage) -> Decimal:
        if usage.cost_usd is None:
            return reserved  # Unknown/error costs retain their full reservation.
        if usage.cost_usd > reserved:
            # Never hide an unexpected actual charge or refund an uncertain failed attempt.
            self.charged += usage.cost_usd - reserved
            self.overrun = True
        else:
            self.charged -= reserved - usage.cost_usd
        return usage.cost_usd
