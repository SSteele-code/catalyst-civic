"""Deterministic review-engine runtime for structured weak pages."""

from .service import ReviewHandshakeService, create_http_handler, dispatch_review_engine_handshake

__all__ = ["ReviewHandshakeService", "create_http_handler", "dispatch_review_engine_handshake"]
