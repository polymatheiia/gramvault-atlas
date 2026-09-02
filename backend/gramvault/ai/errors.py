"""AI-layer exception hierarchy.

Its own leaf module (no imports) so both `gramvault.ai.ollama_client` and
`gramvault.ai.providers.*` can raise/catch the same types without an
import cycle.
"""

from __future__ import annotations


class ProviderError(Exception):
    """Any provider-layer failure."""


class ProviderNotReadyError(ProviderError):
    """The provider can't serve requests right now: the local server is
    down, a required model isn't pulled, or an API key is missing/invalid.
    Routes translate this to HTTP 503."""


class ProviderCapabilityError(ProviderError):
    """The provider doesn't implement the requested task at all."""
