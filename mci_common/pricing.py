# $/1K tokens, approximate. The Sonnet rows use Sonnet's published
# per-1K rate; the Haiku rows use Haiku's. Confirm against the Bedrock
# pricing page before trusting this for anything beyond a rough
# estimate; it is explicitly not guaranteed current.
PRICING = {
    "us.anthropic.claude-sonnet-4-6": {"input": 0.003, "output": 0.015},
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": {"input": 0.0008, "output": 0.004},
    # Claude Code/Copilot report bare model IDs (no Bedrock "us.anthropic."
    # prefix or version suffix) in their OTLP payloads. Added so
    # OTLP-sourced sessions get a real rate instead of silently falling
    # back to DEFAULT_PRICING's Sonnet-shaped numbers for every model.
    "claude-sonnet-4-6": {"input": 0.003, "output": 0.015},
    "claude-sonnet-4-5-20250929": {"input": 0.003, "output": 0.015},
    "claude-haiku-4-5-20251001": {"input": 0.0008, "output": 0.004},
    "claude-opus-4-6": {"input": 0.015, "output": 0.075},
    "gpt-4o": {"input": 0.0025, "output": 0.01},
    "gpt-4.1": {"input": 0.002, "output": 0.008},
}
DEFAULT_PRICING = {"input": 0.003, "output": 0.015}


def estimate_cost(model_id, input_tokens, output_tokens):
    rates = PRICING.get(model_id, DEFAULT_PRICING)
    return round((input_tokens / 1000) * rates["input"] + (output_tokens / 1000) * rates["output"], 6)
