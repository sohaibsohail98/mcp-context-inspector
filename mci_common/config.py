DEFAULT_REGION = "us-east-1"
DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-6"

# Sonnet 4.6 and Haiku 4.5 both confirmed at 200K context on their current
# model cards — single source of truth for context-window math (was
# previously duplicated as a hardcoded 200_000 in web/chat.js).
CONTEXT_WINDOW_TOKENS = 200_000
