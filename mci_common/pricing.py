# $/1K tokens — approximate, matches the closest verified reference
# (Sonnet 4.5's published rate). Confirm against the Bedrock pricing
# page before trusting this for anything beyond a rough estimate; it is
# explicitly not guaranteed current.
PRICING = {
    "us.anthropic.claude-sonnet-4-6": {"input": 0.003, "output": 0.015},
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": {"input": 0.0008, "output": 0.004},
}
DEFAULT_PRICING = {"input": 0.003, "output": 0.015}


def estimate_cost(model_id, input_tokens, output_tokens):
    rates = PRICING.get(model_id, DEFAULT_PRICING)
    return round(
        (input_tokens / 1000) * rates["input"] + (output_tokens / 1000) * rates["output"], 6
    )
