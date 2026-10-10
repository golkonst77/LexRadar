"""Only production decision entry point; this MVP cannot establish trusted legal GO."""

from .service import decide_production, prepare_review

__all__ = ["decide_production", "prepare_review"]
