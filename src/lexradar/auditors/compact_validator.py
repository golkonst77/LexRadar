"""Compact responses prove quote occurrence only, never legal violations."""

from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from ..models import Model
from ..primary.compact import CompactPacket, verify_quote, verify_quotes
from .validator import ResultError


class Observation(Model):
    excerpt: str = Field(min_length=1, max_length=100)
    quote: str = Field(min_length=1, max_length=5000)
    hypothesis: str = Field(min_length=1, max_length=2000)
    status: Literal["potential", "unverifiable"]


class CompactResult(Model):
    auditor: Literal["A"]
    observations: list[Observation] = Field(max_length=30)
    checks: list[str] = Field(min_length=1, max_length=30)


def validate_compact(content: str, root: Path, packet: CompactPacket) -> CompactResult:
    try:
        result = CompactResult.model_validate_json(content)
        verify_quotes(root, packet)
        for observation in result.observations:
            verify_quote(packet, observation.excerpt, observation.quote)
        return result
    except (ValueError, KeyError, ValidationError) as exc:
        raise ResultError("invalid_compact_response_or_quote") from exc
