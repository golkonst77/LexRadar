"""Independent technical facts collector; no Gateway integration."""

from .crawl import CrawlOptions
from .models import CollectionResult, Limits
from .runner import collect

__all__ = ["CollectionResult", "CrawlOptions", "Limits", "collect"]
