DEFAULT_REGION = "us-east-1"

# Sonnet 4.6 and Haiku 4.5 both confirmed at 200K context on their current
# model cards — single source of truth for context-window math.
CONTEXT_WINDOW_TOKENS = 200_000

# agent/runtime.py (the Bedrock agent's own code, a sibling repo — not
# present here) uses a chars-per-token estimate to size context_blocks
# without a real tokenizer on hand. The OTLP mappers need the same kind
# of estimate for content that never carries an exact token count
# (e.g. a raw request body's tool spec, before any usage block exists
# for it) — ~4 chars/token is the commonly-cited rough average for
# English text tokenized by Anthropic/OpenAI-style BPE tokenizers, not
# a measured constant. Prefer an exact count from a response's own
# `usage` block wherever one exists; only fall back to this for content
# with no such block.
CHARS_PER_TOKEN_ESTIMATE = 4
