"""Model provider abstraction. All agents share ONE provider instance (one resident model)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class ProviderError(Exception):
    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class ProviderTimeout(ProviderError):
    def __init__(self, message: str = "model request timed out"):
        super().__init__(message, retryable=True)


class ProviderUnavailable(ProviderError):
    """Connection refused / 5xx / model still loading."""

    def __init__(self, message: str = "model server unavailable"):
        super().__init__(message, retryable=True)


@dataclass
class ChatMessage:
    role: str
    content: str

    def as_dict(self) -> dict:
        return {"role": self.role, "content": self.content}


@dataclass
class ChatResult:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: str = "stop"


class ModelProvider(ABC):
    @abstractmethod
    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        max_tokens: int = 4096,
        temperature: float = 0.2,
        json_mode: bool = False,
    ) -> ChatResult: ...

    @abstractmethod
    async def health(self) -> tuple[bool, str]:
        """(ready, human readable detail). Must never raise."""
