"""Anthropic provider adapter (Claude Sonnet 4.6, Opus 4.6) via OAuth"""

import json
import time
from typing import Any

import aiohttp
import structlog

from .base import BaseProvider, ProviderConfig, ProviderResponse

logger = structlog.get_logger(__name__)


class AnthropicProvider(BaseProvider):
    """
    Anthropic provider using OAuth Access Token (sk-ant-oat01-...).

    Supports:
    - Claude Sonnet 4.6, Opus 4.6
    - Extended thinking
    - Prompt caching
    - Tool use
    - Structured output via system prompt enforcement
    """

    API_BASE = "https://api.anthropic.com/v1"
    API_VERSION = "2023-06-01"

    def __init__(self, config: ProviderConfig, auth_token: str):
        self.config = config
        self.auth_token = auth_token
        self.session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session"""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def close(self):
        """Close the HTTP session"""
        if self.session and not self.session.closed:
            await self.session.close()

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        response_format: dict | None = None,
        reasoning_effort: str | None = None,
        max_tokens: int | None = None,
    ) -> ProviderResponse:
        """Complete a prompt using Anthropic Messages API"""

        start_time = time.time()
        session = await self._get_session()

        # Extract system message if present
        system_content = None
        user_messages = []
        for msg in messages:
            if msg.get("role") == "system":
                system_content = msg.get("content", "")
            else:
                user_messages.append(msg)

        # Calculate thinking budget first (if applicable)
        thinking_budget = 0
        if "opus" in self.config.model.lower() or "sonnet" in self.config.model.lower():
            if reasoning_effort:
                budget_map = {"low": 2000, "medium": 5000, "high": 10000}
                thinking_budget = budget_map.get(reasoning_effort, 5000)
            else:
                # Default budget for thinking
                thinking_budget = 5000

        # Calculate max_tokens (must be > thinking_budget)
        requested_max_tokens = max_tokens or self.config.max_tokens_out
        if thinking_budget > 0:
            # Ensure max_tokens > thinking_budget
            actual_max_tokens = max(requested_max_tokens, thinking_budget + 1000)
        else:
            actual_max_tokens = requested_max_tokens

        # Build request body
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": user_messages,
            "max_tokens": actual_max_tokens,
        }

        # Add system prompt with caching if present
        if system_content:
            body["system"] = [
                {
                    "type": "text",
                    "text": system_content,
                    "cache_control": {"type": "ephemeral"}  # Enable prompt caching
                }
            ]

        # Add response format hint to system if requested
        if response_format:
            json_instruction = (
                "\n\nIMPORTANT: You MUST respond with"
                " valid JSON matching the requested"
                " schema. No other text."
            )
            if system_content:
                body["system"][0]["text"] += json_instruction
            else:
                body["system"] = [
                    {
                        "type": "text",
                        "text": json_instruction,
                        "cache_control": {"type": "ephemeral"}
                    }
                ]

        # Add extended thinking if model supports it
        if thinking_budget > 0:
            body["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}

        # Add tools if provided
        if tools:
            body["tools"] = tools

        # Make request
        headers = {
            "x-api-key": self.auth_token,
            "anthropic-version": self.API_VERSION,
            "content-type": "application/json",
        }

        try:
            timeout = aiohttp.ClientTimeout(total=self.config.timeout_ms / 1000)
            async with session.post(
                f"{self.API_BASE}/messages",
                headers=headers,
                json=body,
                timeout=timeout,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

                # Extract response text
                text = ""
                for content_block in data.get("content", []):
                    if content_block.get("type") == "text":
                        text += content_block.get("text", "")

                # Parse JSON if response_format requested
                structured = None
                if response_format and text:
                    try:
                        # Try to extract JSON from markdown code blocks
                        if "```json" in text:
                            json_start = text.find("```json") + 7
                            json_end = text.find("```", json_start)
                            json_text = text[json_start:json_end].strip()
                        elif "```" in text:
                            json_start = text.find("```") + 3
                            json_end = text.find("```", json_start)
                            json_text = text[json_start:json_end].strip()
                        else:
                            json_text = text.strip()

                        structured = json.loads(json_text)
                    except json.JSONDecodeError as e:
                        logger.warning("failed_to_parse_json", error=str(e), text=text[:200])

                # Extract token usage
                usage = data.get("usage", {})
                input_tokens = usage.get("input_tokens", 0)
                output_tokens = usage.get("output_tokens", 0)
                cache_hit = usage.get("cache_read_input_tokens", 0) > 0

                latency_ms = (time.time() - start_time) * 1000

                logger.info(
                    "anthropic_complete",
                    model=self.config.model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=latency_ms,
                    cache_hit=cache_hit,
                )

                return ProviderResponse(
                    text=text,
                    structured=structured,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=latency_ms,
                    cache_hit=cache_hit,
                    model=self.config.model,
                    provider_state_ref=data.get("id"),
                )

        except aiohttp.ClientResponseError as e:
            logger.error(
                "anthropic_api_error",
                status=e.status,
                message=str(e),
                model=self.config.model,
            )
            raise
        except Exception as e:
            logger.error(
                "anthropic_error",
                error=str(e),
                model=self.config.model,
            )
            raise

    async def health_check(self) -> bool:
        """Check if Anthropic API is accessible"""
        try:
            session = await self._get_session()

            # Simple test request without extended thinking
            body = {
                "model": self.config.model,
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 10,
            }

            headers = {
                "x-api-key": self.auth_token,
                "anthropic-version": self.API_VERSION,
                "content-type": "application/json",
            }

            timeout = aiohttp.ClientTimeout(total=10)
            async with session.post(
                f"{self.API_BASE}/messages",
                headers=headers,
                json=body,
                timeout=timeout,
            ) as resp:
                if resp.status == 200:
                    return True
                else:
                    error_text = await resp.text()
                    logger.warning(
                        "anthropic_health_check_failed",
                        status=resp.status,
                        error=error_text[:200],
                    )
                    return False
        except Exception as e:
            logger.warning("anthropic_health_check_failed", error=str(e))
            return False
