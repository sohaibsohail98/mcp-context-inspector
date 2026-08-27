DEFAULT_REGION = "us-east-1"

# Sonnet 4.6 and Haiku 4.5 both confirmed at 200K context on their current
# model cards. Single source of truth for context-window math.
CONTEXT_WINDOW_TOKENS = 200_000

# The OTLP mappers need a size estimate for content that never carries an
# exact token count (e.g. a raw request body's tool spec, before any usage
# block exists for it). ~4 chars/token is the commonly-cited rough average
# for English text tokenized by Anthropic/OpenAI-style BPE tokenizers, not
# a measured constant. Prefer an exact count from a response's own
# `usage` block wherever one exists; only fall back to this for content
# with no such block.
CHARS_PER_TOKEN_ESTIMATE = 4
