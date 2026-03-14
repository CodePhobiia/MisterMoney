"""Base classes for provider adapters"""

from abc import ABC, abstractmethod
from typing import Literal

from pydantic import BaseModel


class ProviderConfig(BaseModel):
    """Configuration for a provider instance"""

    provider: Literal["anthropic", "openai", "google"]
    model: str
    max_tokens_out: int = 4096
    timeout_ms: int = 30000
    rate_limit_rpm: int = 60


class ProviderResponse(BaseModel):
    """Response from a provider completion call"""

    text: str
    structured: dict | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    provider_state_ref: str | None = None
    cache_hit: bool = False
    model: str = ""


class BaseProvider(ABC):
    """Abstract base class for all provider adapters"""

    config: ProviderConfig

    @abstractmethod
    async def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        response_format: dict | None = None,
        reasoning_effort: str | None = None,
        max_tokens: int | None = None,
    ) -> ProviderResponse:
        """
        Complete a prompt with the provider's model.

        Args:
            messages: List of message dicts with 'role' and 'content'
            tools: Optional list of tool definitions
            response_format: Optional structured output format
            reasoning_effort: Optional reasoning effort level (provider-specific)
            max_tokens: Optional max tokens override

        Returns:
            ProviderResponse with text, tokens, latency, etc.
        """
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """
        Check if the provider is available and credentials are valid.

        Returns:
            True if healthy, False otherwise
        """
        ...
