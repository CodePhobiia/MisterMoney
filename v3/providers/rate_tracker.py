"""Rate limit tracking for provider requests"""

import time
from collections import deque
from typing import Dict, Tuple
import structlog

logger = structlog.get_logger(__name__)


class RateTracker:
    """
    Simple in-memory sliding window rate limiter.
    
    Tracks RPM (requests per minute) and TPM (tokens per minute) per provider.
    Redis integration deferred to Sprint 1.
    """
    
    def __init__(self):
        # provider_id -> deque of (timestamp, token_count)
        self._windows: Dict[str, deque] = {}
        # provider_id -> (rpm_limit, tpm_limit)
        self._limits: Dict[str, Tuple[int, int]] = {}
    
    def set_limits(self, provider_id: str, rpm: int, tpm: int):
        """
        Set rate limits for a provider.
        
        Args:
            provider_id: Provider identifier (e.g., "sonnet", "gpt54pro")
            rpm: Requests per minute limit
            tpm: Tokens per minute limit
        """
        self._limits[provider_id] = (rpm, tpm)
        if provider_id not in self._windows:
            self._windows[provider_id] = deque()
        
        logger.info("rate_limits_set", provider_id=provider_id, rpm=rpm, tpm=tpm)
    
    def _clean_old_entries(self, provider_id: str, now: float):
        """Remove entries older than 60 seconds"""
        window = self._windows.get(provider_id)
        if not window:
            return
        
        cutoff = now - 60
        while window and window[0][0] < cutoff:
            window.popleft()
    
    def check_rate(self, provider_id: str, estimated_tokens: int = 0) -> bool:
        """
        Check if a request would exceed rate limits.
        
        Args:
            provider_id: Provider identifier
            estimated_tokens: Estimated token count for the request
            
        Returns:
            True if under limits, False if would exceed
        """
        if provider_id not in self._limits:
            # No limits set, allow by default
            return True
        
        rpm_limit, tpm_limit = self._limits[provider_id]
        now = time.time()
        
        # Clean old entries
        self._clean_old_entries(provider_id, now)
        
        window = self._windows.get(provider_id, deque())
        
        # Count current usage
        current_requests = len(window)
        current_tokens = sum(entry[1] for entry in window)
        
        # Check limits
        if current_requests >= rpm_limit:
            logger.warning(
                "rate_limit_exceeded",
                provider_id=provider_id,
                limit_type="rpm",
                current=current_requests,
                limit=rpm_limit,
            )
            return False
        
        if tpm_limit > 0 and (current_tokens + estimated_tokens) > tpm_limit:
            logger.warning(
                "rate_limit_exceeded",
                provider_id=provider_id,
                limit_type="tpm",
                current=current_tokens,
                estimated=estimated_tokens,
                limit=tpm_limit,
            )
            return False
        
        return True
    
    def record_request(self, provider_id: str, token_count: int = 0):
        """
        Record a completed request.
        
        Args:
            provider_id: Provider identifier
            token_count: Actual token count used
        """
        if provider_id not in self._windows:
            self._windows[provider_id] = deque()
        
        now = time.time()
        self._windows[provider_id].append((now, token_count))
        
        # Clean old entries
        self._clean_old_entries(provider_id, now)
    
    def get_current_usage(self, provider_id: str) -> Tuple[int, int]:
        """
        Get current usage for a provider.
        
        Args:
            provider_id: Provider identifier
            
        Returns:
            Tuple of (current_requests, current_tokens) in the last 60 seconds
        """
        if provider_id not in self._windows:
            return (0, 0)
        
        now = time.time()
        self._clean_old_entries(provider_id, now)
        
        window = self._windows[provider_id]
        return (len(window), sum(entry[1] for entry in window))
    
    def wait_time_seconds(self, provider_id: str, estimated_tokens: int = 0) -> float:
        """
        Calculate how long to wait before next request would be allowed.
        
        Args:
            provider_id: Provider identifier
            estimated_tokens: Estimated token count for next request
            
        Returns:
            Seconds to wait (0 if can proceed now)
        """
        if provider_id not in self._limits:
            return 0.0
        
        rpm_limit, tpm_limit = self._limits[provider_id]
        now = time.time()
        
        self._clean_old_entries(provider_id, now)
        
        window = self._windows.get(provider_id, deque())
        if not window:
            return 0.0
        
        # Check if we're over RPM
        if len(window) >= rpm_limit:
            # Need to wait until oldest request is 60 seconds old
            oldest_timestamp = window[0][0]
            wait_until = oldest_timestamp + 60
            return max(0, wait_until - now)
        
        # Check if we're over TPM
        current_tokens = sum(entry[1] for entry in window)
        if tpm_limit > 0 and (current_tokens + estimated_tokens) > tpm_limit:
            # Need to wait until enough tokens drop off
            # Find the earliest entry that would bring us under limit
            target_tokens = tpm_limit - estimated_tokens
            cumulative = 0
            for timestamp, tokens in reversed(window):
                cumulative += tokens
                if current_tokens - cumulative <= target_tokens:
                    wait_until = timestamp + 60
                    return max(0, wait_until - now)
        
        return 0.0
