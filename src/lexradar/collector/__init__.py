"""Independent technical facts collector; no Gateway integration."""

from .models import CollectionResult, Limits
from .runner import collect

__all__ = ["CollectionResult", "Limits", "collect"]
