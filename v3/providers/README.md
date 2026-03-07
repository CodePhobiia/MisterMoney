# V3 Provider Access Layer

OAuth-based provider adapters for MisterMoney V3 Resolution Intelligence.

## Status

### Working ✅
- **Anthropic Claude Sonnet 4.6** - Extended thinking, prompt caching, tool use
- **Anthropic Claude Opus 4.6** - Extended thinking, prompt caching, tool use

### Not Working ❌
- **OpenAI GPT-5.4** - API format mismatch ("Input must be a list" error)
- **OpenAI GPT-5.4-pro** - Not supported in current ChatGPT Pro subscription
- **Google Gemini 3.1 Pro** - OAuth token expired, needs re-authentication

## Usage

```python
from v3.providers import ProviderRegistry

# Initialize registry
registry = ProviderRegistry()
await registry.initialize()

# Get provider
provider = await registry.get("sonnet")

# Make request
response = await provider.complete(
    messages=[
        {"role": "system", "content": "You are a market analyst."},
        {"role": "user", "content": "Analyze this market..."}
    ],
    max_tokens=4096,
    reasoning_effort="medium",  # low/medium/high
)

print(response.text)
print(f"Tokens: {response.input_tokens} in, {response.output_tokens} out")
print(f"Latency: {response.latency_ms}ms")
print(f"Cache hit: {response.cache_hit}")

# Cleanup
await registry.close_all()
```

## Features

### Anthropic Providers
- ✅ Extended thinking with configurable budget
- ✅ Prompt caching (automatically enabled for system prompts)
- ✅ Tool use support
- ✅ Structured output (via system prompt enforcement)
- ✅ Health checks

### Rate Limiting
In-memory sliding window tracking (RPM/TPM). Redis integration deferred to Sprint 1.

## Testing

```bash
python3 -m v3.providers.test_providers
```

## Architecture

- `base.py` - Abstract base classes (ProviderConfig, ProviderResponse, BaseProvider)
- `anthropic_adapter.py` - Anthropic Claude integration
- `openai_adapter.py` - OpenAI Codex integration (currently broken)
- `google_adapter.py` - Google Gemini integration (token expired)
- `registry.py` - Provider instance manager
- `rate_tracker.py` - Rate limit tracking
- `test_providers.py` - Live integration tests

## Next Steps (Sprint 1+)

1. Fix OpenAI Codex API format (investigate correct request structure)
2. Re-authenticate Google Gemini CLI OAuth
3. Add Redis-based rate limiting
4. Add retry logic with exponential backoff
5. Add circuit breakers for failing providers
6. Add metrics collection (latency, tokens, costs)
