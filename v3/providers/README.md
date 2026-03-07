# V3 Provider Access Layer

OAuth-based provider adapters for MisterMoney V3 Resolution Intelligence.

## Status

### Working ✅
- **Anthropic Claude Sonnet 4.6** — Hot-path triage, prompt caching, tool use
- **Anthropic Claude Opus 4.6** — Rule lawyer, adversarial challenger
- **OpenAI GPT-5.4** — Online judge, orchestrator (Codex Responses API)
- **Google Gemini 3 Pro Preview** — Long-context dossier (Cloud Code Assist OAuth)

### Unavailable ⚠️
- **OpenAI GPT-5.4-pro** — Not supported via Codex ChatGPT endpoint ("not supported when using Codex with a ChatGPT account"). Needs standard OpenAI API key for async adjudication.

## Usage

```python
from v3.providers import ProviderRegistry

registry = ProviderRegistry()
await registry.initialize()

# Available roles: "sonnet", "opus", "gpt54", "gemini"
provider = await registry.get("sonnet")

response = await provider.complete(
    messages=[
        {"role": "system", "content": "You are a market analyst."},
        {"role": "user", "content": "Analyze this market..."},
    ],
    reasoning_effort="medium",  # low/medium/high
)

print(response.text)
print(f"Tokens: {response.input_tokens} in, {response.output_tokens} out")
print(f"Latency: {response.latency_ms:.0f}ms")

await registry.close_all()
```

## Provider Details

### Anthropic (OAT token, no refresh needed)
- Extended thinking with configurable budget
- Prompt caching (auto-enabled for system prompts)
- Tool use support
- Token: `sk-ant-oat01-...` from auth-profiles.json

### OpenAI (Codex OAuth, auto-refresh)
- Codex Responses API: `chatgpt.com/backend-api/codex/responses`
- Input must be list of `{role, content}`, `store: false` required
- SSE streaming response parsing

### Google (CCA OAuth, auto-refresh)
- Cloud Code Assist: `cloudcode-pa.googleapis.com/v1internal:generateContent`
- CCA wraps request in `{project, model, request: {contents, generationConfig}}`
- Model names without `models/` prefix
- 429 rate limits = healthy but throttled
- Token auto-refreshed via `oauth2.googleapis.com/token`

### Rate Limiting
In-memory sliding window tracking (RPM/TPM). Redis integration deferred to Sprint 1.

## Architecture

- `base.py` — Abstract base classes
- `anthropic_adapter.py` — Anthropic Claude integration
- `openai_adapter.py` — OpenAI Codex integration
- `google_adapter.py` — Google CCA integration
- `registry.py` — Provider instance manager
- `rate_tracker.py` — Rate limit tracking
- `test_providers.py` — Live integration tests

## Key Lessons

1. Codex API: `input` must be list, `store: false` required, no `max_output_tokens`
2. CCA API: body is `{project, model, request: {...}}` not flat `{contents, generationConfig}`
3. CCA model names: `gemini-3-pro-preview` not `models/gemini-3-pro-preview`
4. CCA rate limit (429) during health check = provider is reachable, not broken
5. GPT-5.4-pro not available via Codex endpoint — needs standard API key
