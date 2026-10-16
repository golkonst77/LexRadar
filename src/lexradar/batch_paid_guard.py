"""Durable, conservative cap for Single A calls, including retries."""

from decimal import Decimal

from .auditors.budget import Budget
from .auditors.provider import ProviderError
from .batch import write_json


class CappedProvider:
    def __init__(self, provider, directory, max_calls, budget):
        self.provider = provider
        self.directory = directory
        self.max_calls = max_calls
        self.calls = 0
        self.budget = Budget(Decimal(budget))

    def complete(self, settings, messages):
        if self.calls >= self.max_calls:
            raise ProviderError("batch_max_ai_calls_reached")
        reserved = self.budget.reserve(settings, messages)
        self.calls += 1
        # Persist before crossing the external boundary. An uncertain attempt is never replayed.
        write_json(
            self.directory / "paid-attempt.json",
            {
                "attempts_reserved": self.calls,
                "budget_reserved_usd": str(self.budget.charged),
                "state": "request_may_have_been_sent",
                "automatic_retry_on_resume": False,
            },
        )
        reply = self.provider.complete(settings, messages)
        self.budget.settle(reserved, reply.usage)
        return reply
