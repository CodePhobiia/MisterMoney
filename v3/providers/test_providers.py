"""Live integration test for all provider adapters"""

import asyncio
import json

import structlog

from .registry import ProviderRegistry

# Configure structlog for readable output
structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ]
)

logger = structlog.get_logger(__name__)


async def test_provider(registry: ProviderRegistry, role: str):
    """Test a single provider"""

    print(f"\n{'=' * 60}")
    print(f"Testing {role.upper()}")
    print('=' * 60)

    provider = await registry.get(role)

    if not provider:
        print(f"❌ Provider '{role}' not available")
        return False

    print(f"✓ Provider initialized: {provider.config.model}")

    # Test message
    messages = [
        {
            "role": "system",
            "content": (
                "You are a helpful assistant."
                " Always respond in valid JSON format"
                " when requested."
            )
        },
        {
            "role": "user",
            "content": (
                'Say hello in JSON format with fields:'
                ' "greeting" (string) and "timestamp"'
                ' (current time as string).'
            )
        }
    ]

    try:
        # Make request
        print("Sending test request...")

        # Use lower reasoning effort for testing
        reasoning_effort = "low" if role in ["gpt54", "gpt54pro"] else None

        response = await provider.complete(
            messages=messages,
            response_format={"type": "json_object"} if role in ["gpt54", "gpt54pro"] else None,
            reasoning_effort=reasoning_effort,
        )

        # Print results
        print("\n✓ Request successful!")
        print(f"  Model: {response.model}")
        print(f"  Latency: {response.latency_ms:.2f}ms")
        print(f"  Input tokens: {response.input_tokens}")
        print(f"  Output tokens: {response.output_tokens}")
        print(f"  Cache hit: {response.cache_hit}")

        if response.provider_state_ref:
            print(f"  State ref: {response.provider_state_ref}")

        print("\nResponse text:")
        print(f"  {response.text[:200]}{'...' if len(response.text) > 200 else ''}")

        # Try to parse as JSON
        if response.structured:
            print("\n✓ Structured output parsed successfully:")
            print(f"  {json.dumps(response.structured, indent=2)}")
        else:
            # Try to parse text as JSON
            try:
                parsed = json.loads(response.text)
                print("\n✓ Response is valid JSON:")
                print(f"  {json.dumps(parsed, indent=2)}")
            except json.JSONDecodeError:
                print("\n⚠ Response is not JSON (this may be expected for some providers)")

        return True

    except Exception as e:
        print(f"\n❌ Error: {str(e)}")
        logger.exception("provider_test_failed", role=role)
        return False


async def main():
    """Run all provider tests"""

    print("\n" + "=" * 60)
    print("MisterMoney V3 Provider Integration Test")
    print("=" * 60)

    # Initialize registry
    print("\nInitializing provider registry...")
    registry = ProviderRegistry()
    await registry.initialize()

    available = list(registry.providers.keys())
    print("\n✓ Registry initialized")
    print(f"  Available providers: {', '.join(available) if available else 'none'}")

    if not available:
        print("\n❌ No providers available. Check auth-profiles.json files.")
        return

    # Test each provider
    results = {}

    # Test order: fastest to slowest
    test_order = ["sonnet", "gpt54", "gemini", "opus", "gpt54pro"]

    for role in test_order:
        if role in available:
            success = await test_provider(registry, role)
            results[role] = success

            # Small delay between tests
            if role != test_order[-1]:
                await asyncio.sleep(1)

    # Summary
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)

    for role, success in results.items():
        status = "✓ PASS" if success else "✗ FAIL"
        print(f"  {role:12s} {status}")

    total = len(results)
    passed = sum(1 for s in results.values() if s)

    print(f"\nTotal: {passed}/{total} providers passed")

    # Cleanup
    await registry.close_all()

    print("\n✓ Test complete!\n")


if __name__ == "__main__":
    asyncio.run(main())
