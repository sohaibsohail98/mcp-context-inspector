# Demo capture: candidate takes

`make demo-candidates` renders one eight second take per entry in
`EXPAND_VARIANTS` (`scripts/demo_capture.py`), varying only which context
block gets expanded in the 4.5s-6.5s reveal step. Everything else in the
choreography (timing, zoom, cursor, session choice) is identical across
takes, by design, so the comparison isolates that one decision.

Candidates, all against `demo-session-12` ("do a full multi-signal
investigation of the checkout-api incident"):

- **tools**: expands "Tool specs (5 tools)", showing the raw JSON tool
  schema. Genuinely surprising (most people don't think of tool specs as
  a token cost), but the block is long enough that the final frame cuts
  off mid-schema, mid-object, with no closing brace visible. Reads as
  unfinished on a still that has to stand alone before playback starts.
- **system**: expands "System prompt", showing the full injected
  constraints (turn limit, anti-fabrication rule, "say so explicitly if
  inconclusive"). Fully visible, no cutoff. Reasonable, but a system
  prompt containing rules is the expected answer to "what's using my
  context," not the unexpected one the brief asks for.
- **tool_result**: expands "Tool result: list_services", showing a
  complete, short JSON payload (latency percentiles, error rate, and a
  plain-English `note` tying the anomaly to a deploy). Fully visible.
  Because it's short, more of the surrounding block sequence stays on
  screen too (system prompt, tool specs, user prompt, tool call, this
  result, next turn's reasoning), so the still communicates the whole
  shape of one turn's context, not just one isolated line item.

## Chosen: tool_result

Picked as the shipped `docs/demo.mp4` / `docs/demo.gif` (see the
Makefile's `VARIANT` default and `demo_capture.py --variant`'s default,
both `tool_result`) because:

1. It is the only candidate whose expanded content is fully visible in
   the final frame, no mid-object cutoff. The last frame is the one
   viewers sit on and the one that shows in a feed before playback
   starts, so legibility there matters more than anywhere else in the
   choreography.
2. It reads as evidence, not just an accounting fact: the `note` field
   directly supports the session's own narrative (checkout-api p99
   regression), so a viewer immediately understands the tool has grounded
   an answer in something specific and re-inspectable, which is the
   product's actual pitch.
3. Because the payload is compact, the frame also shows more of the
   preceding block sequence (system prompt through to the next turn's
   reasoning), giving a fuller picture of "everything that entered the
   model's context window, in order" than a single expanded block does
   on its own.

The other two takes are kept locally by `make demo-candidates` under the
gitignored `docs/_scratch/` for anyone who wants to compare directly;
they are not committed.
