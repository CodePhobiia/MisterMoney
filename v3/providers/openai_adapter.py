"""OpenAI provider adapter (GPT-5.4, GPT-5.4-pro) via Codex OAuth + official API fallback"""

import json
import os
import time
from typing import Any

import aiohttp
import structlog

from .base import BaseProvider, ProviderConfig, ProviderResponse

logger = structlog.get_logger(__name__)


class OpenAIProvider(BaseProvider):
    """
    OpenAI provider using ChatGPT Pro OAuth token (Codex Responses API).
    Falls back to official OpenAI API (api.openai.com/v1) if available.

    Supports:
    - GPT-5.4 (online judge, reasoning: low/medium/high)
    - GPT-5.4-pro (async adjudicator, reasoning: high/xhigh)
    - SSE streaming response parsing
    - Official API fallback when Codex endpoint fails
    """

    API_BASE = "https://chatgpt.com/backend-api/codex"
    OFFICIAL_API_BASE = "https://api.openai.com/v1"

    def __init__(self, config: ProviderConfig, auth_token: str):
        self.config = config
        self.auth_token = auth_token
        self.official_api_key = os.getenv("OPENAI_API_KEY", "")
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

    def _parse_sse_line(self, line: str) -> dict | None:
        """Parse a single SSE data line"""
        if not line.startswith("data: "):
            return None

        data_str = line[6:].strip()
        if data_str == "[DONE]":
            return None

        try:
            return json.loads(data_str)
        except json.JSONDecodeError:
            return None

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        response_format: dict | None = None,
        reasoning_effort: str | None = None,
        max_tokens: int | None = None,
    ) -> ProviderResponse:
        """Complete a prompt using OpenAI Codex Responses API"""

        start_time = time.time()
        session = await self._get_session()

        # Separate system instructions from conversation messages
        system_content = ""
        input_messages = []

        for msg in messages:
            if msg.get("role") == "system":
                system_content += msg.get("content", "") + "\n"
            else:
                input_messages.append({"role": msg["role"], "content": msg.get("content", "")})

        if not input_messages:
            input_messages = [{"role": "user", "content": ""}]

        # Build request body — Codex API requires input as list, store=false, no max_output_tokens
        body: dict[str, Any] = {
            "model": self.config.model,
            "instructions": system_content.strip(),
            "input": input_messages,
            "stream": True,
            "store": False,
        }

        # Add reasoning effort
        if reasoning_effort:
            # For GPT-5.4-pro, use at least "high"
            if "pro" in self.config.model.lower():
                if reasoning_effort in ["low", "medium"]:
                    reasoning_effort = "high"
            body["reasoning"] = {"effort": reasoning_effort}
        elif "pro" in self.config.model.lower():
            # Default to high for pro
            body["reasoning"] = {"effort": "high"}
        else:
            # Default to medium for standard
            body["reasoning"] = {"effort": "medium"}

        # Add response format if requested
        if response_format:
            body["response_format"] = response_format

        # Add tools if provided
        if tools:
            body["tools"] = tools

        # Make request
        headers = {
            "Authorization": f"Bearer {self.auth_token}",
            "Content-Type": "application/json",
        }

        try:
            # Higher timeout for GPT-5.4-pro
            timeout_seconds = self.config.timeout_ms / 1000
            if "pro" in self.config.model.lower():
                timeout_seconds = max(timeout_seconds, 120)

            timeout = aiohttp.ClientTimeout(total=timeout_seconds)

            async with session.post(
                f"{self.API_BASE}/responses",
                headers=headers,
                json=body,
                timeout=timeout,
            ) as resp:
                resp.raise_for_status()

                # Parse SSE stream
                final_response = None
                async for line in resp.content:
                    line_str = line.decode('utf-8').strip()
                    if not line_str:
                        continue

                    event_data = self._parse_sse_line(line_str)
                    if event_data and event_data.get("type") == "response.completed":
                        final_response = event_data
                        break

                if not final_response:
                    raise ValueError("No completed response received from SSE stream")

                # Extract response text — completed event wraps in .response
                resp_obj = final_response.get("response", final_response)
                text = ""
                output_list = resp_obj.get("output", [])
                for output_item in output_list:
                    for content_block in output_item.get("content", []):
                        if content_block.get("type") in ("output_text", "text"):
                            text += content_block.get("text", "")

                # Parse structured output if response_format was requested
                structured = None
                if response_format and text:
                    try:
                        structured = json.loads(text)
                    except json.JSONDecodeError as e:
                        logger.warning("failed_to_parse_json", error=str(e), text=text[:200])

                # Extract token usage
                usage = resp_obj.get("usage", {})
                input_tokens = usage.get("input_tokens", 0)
                output_tokens = usage.get("output_tokens", 0)

                latency_ms = (time.time() - start_time) * 1000

                logger.info(
                    "openai_complete",
                    model=self.config.model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=latency_ms,
                    reasoning_effort=body["reasoning"]["effort"],
                )

                return ProviderResponse(
                    text=text,
                    structured=structured,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=latency_ms,
                    cache_hit=False,
                    model=self.config.model,
                    provider_state_ref=resp_obj.get("id"),
                )

        except (aiohttp.ClientResponseError, Exception) as e:
            logger.warning(
                "openai_codex_failed_trying_official",
                error=str(e),
                model=self.config.model,
                has_official_key=bool(self.official_api_key),
            )
            # Fallback to official OpenAI API if key is available
            if self.official_api_key:
                return await self._complete_official(
                    messages, tools, response_format, reasoning_effort, max_tokens
                )
            raise

    async def _complete_official(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        response_format: dict | None = None,
        reasoning_effort: str | None = None,
        max_tokens: int | None = None,
    ) -> ProviderResponse:
        """Fallback completion via official OpenAI API (api.openai.com/v1)."""
        start_time = time.time()
        session = await self._get_session()

        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
        }
        if max_tokens:
            body["max_tokens"] = max_tokens
        if response_format:
            body["response_format"] = response_format
        if tools:
            body["tools"] = tools

        headers = {
            "Authorization": f"Bearer {self.official_api_key}",
            "Content-Type": "application/json",
        }

        timeout_seconds = self.config.timeout_ms / 1000
        if "pro" in self.config.model.lower():
            timeout_seconds = max(timeout_seconds, 120)
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)

        try:
            async with session.post(
                f"{self.OFFICIAL_API_BASE}/chat/completions",
                headers=headers,
                json=body,
                timeout=timeout,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

                text = ""
                choices = data.get("choices", [])
                if choices:
                    text = choices[0].get("message", {}).get("content", "")

                structured = None
                if response_format and text:
                    try:
                        structured = json.loads(text)
                    except json.JSONDecodeError:
                        pass

                usage = data.get("usage", {})
                input_tokens = usage.get("prompt_tokens", 0)
                output_tokens = usage.get("completion_tokens", 0)
                latency_ms = (time.time() - start_time) * 1000

                logger.info(
                    "openai_official_complete",
                    model=self.config.model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=latency_ms,
                )

                return ProviderResponse(
                    text=text,
                    structured=structured,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=latency_ms,
                    cache_hit=False,
                    model=self.config.model,
                )
        except Exception as e:
            logger.error("openai_official_api_error", error=str(e))
            raise

    async def health_check(self) -> bool:
        """Check if OpenAI Codex API is accessible"""
        try:
            session = await self._get_session()

            # Simple test request with minimal settings
            body = {
                "model": self.config.model,
                "instructions": "You are a helpful assistant.",
                "input": [{"role": "user", "content": "Say hello"}],
                "stream": True,
                "store": False,
                "reasoning": {"effort": "low"},
            }

            headers = {
                "Authorization": f"Bearer {self.auth_token}",
                "Content-Type": "application/json",
            }

            timeout = aiohttp.ClientTimeout(total=15)
            async with session.post(
                f"{self.API_BASE}/responses",
                headers=headers,
                json=body,
                timeout=timeout,
            ) as resp:
                if resp.status == 200:
                    # Just check we got a 200, don't parse the stream
                    return True
                else:
                    error_text = await resp.text()
                    logger.warning(
                        "openai_health_check_failed",
                        status=resp.status,
                        error=error_text[:200],
                    )
                    return False
        except Exception as e:
            logger.warning("openai_health_check_failed", error=str(e))
            return False
