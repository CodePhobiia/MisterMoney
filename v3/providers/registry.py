"""Provider registry for managing all provider instances"""

import json
from pathlib import Path
from typing import Dict
import structlog

from .base import BaseProvider, ProviderConfig
from .anthropic_adapter import AnthropicProvider
from .openai_adapter import OpenAIProvider
from .google_adapter import GoogleProvider

logger = structlog.get_logger(__name__)


class ProviderRegistry:
    """
    Manages all provider instances. No silent downgrades.
    
    Loads OAuth tokens from auth-profiles.json files and creates
    provider instances with health checks.
    """
    
    def __init__(self):
        self.providers: Dict[str, BaseProvider] = {}
        self._initialized = False
    
    def _load_auth_profile(self, path: str) -> dict:
        """Load auth profile JSON file"""
        try:
            with open(path, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.warning("failed_to_load_auth_profile", path=path, error=str(e))
            return {}
    
    async def initialize(self) -> None:
        """
        Create all provider instances and run health checks.
        
        Loads tokens from:
        - /home/ubuntu/.openclaw/agents/main/agent/auth-profiles.json (Anthropic, OpenAI)
        - /home/ubuntu/.openclaw-lobotomy/agents/main/agent/auth-profiles.json (Google)
        """
        if self._initialized:
            return
        
        logger.info("initializing_provider_registry")
        
        # Load auth profiles
        main_auth = self._load_auth_profile(
            "/home/ubuntu/.openclaw/agents/main/agent/auth-profiles.json"
        )
        lobotomy_auth = self._load_auth_profile(
            "/home/ubuntu/.openclaw-lobotomy/agents/main/agent/auth-profiles.json"
        )
        
        # Create Anthropic providers
        anthropic_profile = main_auth.get("profiles", {}).get("anthropic:default")
        if anthropic_profile:
            token = anthropic_profile.get("token")
            if token:
                # Sonnet 4.6
                try:
                    sonnet_config = ProviderConfig(
                        provider="anthropic",
                        model="claude-sonnet-4-6",
                        max_tokens_out=4096,
                        timeout_ms=30000,
                        rate_limit_rpm=60,
                    )
                    sonnet = AnthropicProvider(sonnet_config, token)
                    self.providers["sonnet"] = sonnet
                    logger.info("created_provider", role="sonnet", model="claude-sonnet-4-6")
                except Exception as e:
                    logger.error("failed_to_create_sonnet", error=str(e))
                
                # Opus 4.6
                try:
                    opus_config = ProviderConfig(
                        provider="anthropic",
                        model="claude-opus-4-6",
                        max_tokens_out=4096,
                        timeout_ms=60000,
                        rate_limit_rpm=30,
                    )
                    opus = AnthropicProvider(opus_config, token)
                    self.providers["opus"] = opus
                    logger.info("created_provider", role="opus", model="claude-opus-4-6")
                except Exception as e:
                    logger.error("failed_to_create_opus", error=str(e))
        
        # Create OpenAI providers
        openai_profile = main_auth.get("profiles", {}).get("openai-codex:default")
        if openai_profile:
            token = openai_profile.get("access")
            if token:
                # GPT-5.4
                try:
                    gpt54_config = ProviderConfig(
                        provider="openai",
                        model="gpt-5.4",
                        max_tokens_out=4096,
                        timeout_ms=30000,
                        rate_limit_rpm=60,
                    )
                    gpt54 = OpenAIProvider(gpt54_config, token)
                    self.providers["gpt54"] = gpt54
                    logger.info("created_provider", role="gpt54", model="gpt-5.4")
                except Exception as e:
                    logger.error("failed_to_create_gpt54", error=str(e))
                
                # GPT-5.4-pro (async adjudicator, high-EV only)
                try:
                    gpt54pro_config = ProviderConfig(
                        provider="openai",
                        model="gpt-5.4-pro",
                        max_tokens_out=8192,
                        timeout_ms=120000,
                        rate_limit_rpm=20,
                    )
                    gpt54pro = OpenAIProvider(gpt54pro_config, token)
                    self.providers["gpt54pro"] = gpt54pro
                    logger.info("created_provider", role="gpt54pro", model="gpt-5.4-pro")
                except Exception as e:
                    logger.error("failed_to_create_gpt54pro", error=str(e))
        
        # Create Google provider (CCA OAuth with auto-refresh)
        google_profile = lobotomy_auth.get("profiles", {}).get("google-gemini-cli:talmerri@gmail.com")
        if google_profile:
            access = google_profile.get("access")
            refresh = google_profile.get("refresh")
            expires = google_profile.get("expires")
            project_id = google_profile.get("projectId")
            
            if all([access, refresh, expires, project_id]):
                try:
                    gemini_config = ProviderConfig(
                        provider="google",
                        model="gemini-3-pro-preview",
                        max_tokens_out=8192,
                        timeout_ms=30000,
                        rate_limit_rpm=60,
                    )
                    gemini = GoogleProvider(
                        gemini_config,
                        access,
                        refresh,
                        expires,
                        project_id,
                    )
                    self.providers["gemini"] = gemini
                    logger.info("created_provider", role="gemini", model="gemini-3-pro-preview")
                except Exception as e:
                    logger.error("failed_to_create_gemini", error=str(e))
        
        # Run health checks
        for role, provider in list(self.providers.items()):
            try:
                healthy = await provider.health_check()
                if healthy:
                    logger.info("provider_healthy", role=role, model=provider.config.model)
                else:
                    logger.warning("provider_unhealthy", role=role, model=provider.config.model)
                    # Remove unhealthy provider
                    del self.providers[role]
            except Exception as e:
                logger.error("health_check_failed", role=role, error=str(e))
                # Remove failed provider
                if role in self.providers:
                    del self.providers[role]
        
        self._initialized = True
        logger.info(
            "provider_registry_initialized",
            available_providers=list(self.providers.keys()),
        )
    
    async def get(self, role: str) -> BaseProvider | None:
        """
        Get provider by role name. Returns None if unavailable (no silent downgrade).
        
        Args:
            role: One of "sonnet", "opus", "gpt54", "gpt54pro", "gemini"
            
        Returns:
            Provider instance or None if unavailable
        """
        if not self._initialized:
            await self.initialize()
        
        return self.providers.get(role)
    
    async def is_available(self, role: str) -> bool:
        """Check if a provider role is available"""
        if not self._initialized:
            await self.initialize()
        
        return role in self.providers
    
    async def close_all(self):
        """Close all provider sessions"""
        for provider in self.providers.values():
            if hasattr(provider, 'close'):
                try:
                    await provider.close()
                except Exception as e:
                    logger.warning("failed_to_close_provider", error=str(e))
