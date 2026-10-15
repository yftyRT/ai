from .base import (
    ChatMessage,
    ChatResult,
    ModelProvider,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
)
from .fake import FakeProvider
from .openai_compat import OpenAICompatProvider

__all__ = [
    "ChatMessage",
    "ChatResult",
    "ModelProvider",
    "ProviderError",
    "ProviderTimeout",
    "ProviderUnavailable",
    "FakeProvider",
    "OpenAICompatProvider",
]


def build_provider(settings):
    """Factory: one shared provider for every agent."""
    if settings.provider == "fake":
        return FakeProvider(model_id=settings.model_id)
    return OpenAICompatProvider(
        settings.model_base_url,
        settings.model_api_key,
        settings.model_id,
        max_concurrent=settings.max_concurrent_model_requests,
        timeout_s=settings.request_timeout_s,
        max_output_tokens=settings.max_output_tokens,
    )
