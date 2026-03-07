"""Google provider adapter (Gemini 3.1 Pro) via Cloud Code Assist OAuth"""

import asyncio
import json
import time
import base64
from typing import Any
import aiohttp
import structlog

from .base import BaseProvider, ProviderConfig, ProviderResponse

logger = structlog.get_logger(__name__)


class GoogleProvider(BaseProvider):
    """
    Google provider using Cloud Code Assist OAuth token.
    
    Supports:
    - Gemini 3.1 Pro
    - Automatic token refresh
    - Exponential backoff for 403 errors
    """
    
    API_BASE = "https://cloudcode-pa.googleapis.com"
    TOKEN_URL = "https://oauth2.googleapis.com/token"
    
    # OAuth credentials from @mariozechner/pi-ai package
    CLIENT_ID = base64.b64decode(
        "NjgxMjU1ODA5Mzk1LW9vOGZ0Mm9wcmRybnA5ZTNhcWY2YXYzaG1kaWIxMzVqLmFwcHMuZ29vZ2xldXNlcmNvbnRlbnQuY29t"
    ).decode()
    CLIENT_SECRET = base64.b64decode(
        "R09DU1BYLTR1SGdNUG0tMW83U2stZ2VWNkN1NWNsWEZzeGw="
    ).decode()
    
    def __init__(
        self,
        config: ProviderConfig,
        access_token: str,
        refresh_token: str,
        expires: int,
        project_id: str,
    ):
        self.config = config
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires = expires
        self.project_id = project_id
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
    
    async def _refresh_token_if_needed(self):
        """Refresh access token if expired or within 5 minutes of expiry"""
        current_time = int(time.time() * 1000)
        
        # Check if token needs refresh (expired or within 5 min)
        if self.expires > current_time + (5 * 60 * 1000):
            return  # Token still valid
        
        logger.info("refreshing_google_token", project_id=self.project_id)
        
        session = await self._get_session()
        
        try:
            async with session.post(
                self.TOKEN_URL,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "client_id": self.CLIENT_ID,
                    "client_secret": self.CLIENT_SECRET,
                    "refresh_token": self.refresh_token,
                    "grant_type": "refresh_token",
                },
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                
                self.access_token = data["access_token"]
                # Keep refresh token if new one not provided
                if "refresh_token" in data:
                    self.refresh_token = data["refresh_token"]
                # Calculate new expiry (current time + expires_in - 5 min buffer)
                self.expires = int(time.time() * 1000) + (data["expires_in"] * 1000) - (5 * 60 * 1000)
                
                logger.info(
                    "google_token_refreshed",
                    expires_in_seconds=data["expires_in"],
                    project_id=self.project_id,
                )
                
        except Exception as e:
            logger.error("google_token_refresh_failed", error=str(e))
            raise
    
    async def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        response_format: dict | None = None,
        reasoning_effort: str | None = None,
        max_tokens: int | None = None,
    ) -> ProviderResponse:
        """Complete a prompt using Google Cloud Code Assist API"""
        
        start_time = time.time()
        
        # Refresh token if needed
        await self._refresh_token_if_needed()
        
        session = await self._get_session()
        
        # Convert messages to Gemini format
        system_parts = []
        contents = []
        for msg in messages:
            if msg.get("role") == "system":
                system_parts.append({"text": msg.get("content", "")})
            else:
                role = "user" if msg.get("role") == "user" else "model"
                contents.append({
                    "role": role,
                    "parts": [{"text": msg.get("content", "")}]
                })
        
        if not contents:
            contents = [{"role": "user", "parts": [{"text": ""}]}]
        
        # Build generation config
        gen_config: dict[str, Any] = {
            "maxOutputTokens": max_tokens or self.config.max_tokens_out,
            "temperature": 0.1,
        }
        
        # Add response format hint if requested
        if response_format:
            gen_config["responseMimeType"] = "application/json"
        
        # Build inner request (CCA wraps in project/model/request)
        inner_request: dict[str, Any] = {
            "contents": contents,
            "generationConfig": gen_config,
        }
        if system_parts:
            inner_request["systemInstruction"] = {"parts": system_parts}
        
        # Build CCA outer body
        body: dict[str, Any] = {
            "project": self.project_id,
            "model": self.config.model,
            "request": inner_request,
            "userAgent": "mistermoney-v3",
            "requestId": f"mm-{int(time.time() * 1000)}",
        }
        
        # Make request with exponential backoff for 403 errors
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "Client-Metadata": json.dumps({
                "ideType": "IDE_UNSPECIFIED",
                "platform": "PLATFORM_UNSPECIFIED",
                "pluginType": "GEMINI",
            }),
        }
        
        max_retries = 3
        backoff_delays = [1, 2, 4, 8]
        
        for attempt in range(max_retries + 1):
            try:
                timeout = aiohttp.ClientTimeout(total=self.config.timeout_ms / 1000)
                
                async with session.post(
                    f"{self.API_BASE}/v1internal:generateContent",
                    headers=headers,
                    json=body,
                    timeout=timeout,
                ) as resp:
                    # Handle 403 with retry
                    if resp.status == 403:
                        error_text = await resp.text()
                        if "lack a Gemini Code Assist license" in error_text or "SECURITY_POLICY_VIOLATED" in error_text:
                            if attempt < max_retries:
                                delay = backoff_delays[attempt]
                                logger.warning(
                                    "google_403_retrying",
                                    attempt=attempt + 1,
                                    delay=delay,
                                    error=error_text[:100],
                                )
                                await asyncio.sleep(delay)
                                continue
                        raise aiohttp.ClientResponseError(
                            request_info=resp.request_info,
                            history=resp.history,
                            status=resp.status,
                            message=error_text,
                        )
                    
                    resp.raise_for_status()
                    data = await resp.json()
                    
                    # Extract response text from nested structure
                    text = ""
                    response_data = data.get("response", {})
                    candidates = response_data.get("candidates", [])
                    if candidates:
                        content = candidates[0].get("content", {})
                        parts = content.get("parts", [])
                        for part in parts:
                            text += part.get("text", "")
                    
                    # Parse structured output if requested
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
                    
                    # Extract token usage (Gemini may not always provide this)
                    usage_metadata = response_data.get("usageMetadata", {})
                    input_tokens = usage_metadata.get("promptTokenCount", 0)
                    output_tokens = usage_metadata.get("candidatesTokenCount", 0)
                    
                    latency_ms = (time.time() - start_time) * 1000
                    
                    logger.info(
                        "google_complete",
                        model=self.config.model,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        latency_ms=latency_ms,
                        project_id=self.project_id,
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
                    
            except aiohttp.ClientResponseError as e:
                if attempt == max_retries:
                    logger.error(
                        "google_api_error",
                        status=e.status,
                        message=str(e),
                        model=self.config.model,
                    )
                    raise
            except Exception as e:
                logger.error(
                    "google_error",
                    error=str(e),
                    model=self.config.model,
                )
                raise
        
        raise RuntimeError("Max retries exceeded for Google API")
    
    async def health_check(self) -> bool:
        """Check if Google Cloud Code Assist API is accessible"""
        try:
            # Refresh token if needed
            await self._refresh_token_if_needed()
            
            session = await self._get_session()
            
            # Simple test request using CCA format
            body = {
                "project": self.project_id,
                "model": self.config.model,
                "request": {
                    "contents": [{"role": "user", "parts": [{"text": "Hello"}]}],
                    "generationConfig": {"maxOutputTokens": 10, "temperature": 0.1},
                },
                "userAgent": "mistermoney-v3",
                "requestId": f"mm-health-{int(time.time() * 1000)}",
            }
            
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
                "Client-Metadata": json.dumps({
                    "ideType": "IDE_UNSPECIFIED",
                    "platform": "PLATFORM_UNSPECIFIED",
                    "pluginType": "GEMINI",
                }),
            }
            
            timeout = aiohttp.ClientTimeout(total=15)
            async with session.post(
                f"{self.API_BASE}/v1internal:generateContent",
                headers=headers,
                json=body,
                timeout=timeout,
            ) as resp:
                if resp.status == 200:
                    return True
                elif resp.status == 429:
                    # Rate limited but reachable — treat as healthy
                    logger.info("google_health_check_rate_limited", status=429)
                    return True
                else:
                    error_text = await resp.text()
                    logger.warning(
                        "google_health_check_failed",
                        status=resp.status,
                        error=error_text[:200],
                    )
                    return False
        except Exception as e:
            logger.warning("google_health_check_failed", error=str(e))
            return False
