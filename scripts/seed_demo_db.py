"""Generates the deterministic demo dataset baked into the public Cloud
Run image (see Dockerfile): no live database, no AWS/Bedrock calls, so
the public deployment has real-looking data to show in the Context
Window Explorer and Recent Sessions without needing anyone's credentials.

Deliberately does NOT go through metrics.store_sqlite.record_session():
that function calls uuid.uuid4()/time.time(), which would make this
script produce different output on every run. A demo dataset with a
stable diff (regenerate → no changes unless the fixtures below changed)
is the point, so this inserts rows directly with fixed IDs and
timestamps using the same schema/statements record_session() uses.

Run from repo root:
    uv run python -m scripts.seed_demo_db [--out demo/metrics.db]
"""

import argparse
import json
import sqlite3
from pathlib import Path

from mci_common.pricing import estimate_cost
from metrics.store_sqlite import _SCHEMA

REPO_ROOT = Path(__file__).parent.parent
DEFAULT_OUT = REPO_ROOT / "demo" / "metrics.db"

# Fixed base epoch (2026-08-01T00:00:00Z) plus a per-session offset,
# deterministic and ordered, not wall-clock.
_BASE_TS = 1785542400.0

SONNET = "us.anthropic.claude-sonnet-4-6"
HAIKU = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

_TOOLS = ["list_services", "get_service_metrics", "search_logs", "get_recent_deployments", "get_cost_breakdown"]

# Full tool-spec JSON, fixed rather than generated from a live schema, so
# expanding the "Tool specs" block in the Context Window Explorer demo
# (see scripts/demo_capture.py's choreography) shows something real
# instead of the "content wasn't captured" placeholder every other
# session's blocks fall back to. This is the block the brief specifically
# wants expanded, since most viewers never think about tool specs as a
# context cost until they see the actual bytes.
_TOOL_SPECS_CONTENT = json.dumps(
    [
        {
            "name": "list_services",
            "description": "List every service this agent has visibility into, with its current health status.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_service_metrics",
            "description": "Fetch latency, error rate, and throughput for one service over a time window.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "service": {"type": "string"},
                    "window_minutes": {"type": "integer", "default": 60},
                },
                "required": ["service"],
            },
        },
        {
            "name": "search_logs",
            "description": "Full-text search over a service's recent logs.",
            "input_schema": {
                "type": "object",
                "properties": {"service": {"type": "string"}, "query": {"type": "string"}},
                "required": ["service", "query"],
            },
        },
        {
            "name": "get_recent_deployments",
            "description": "List deployments to a service in the last 24 hours, with build hash and rollback state.",
            "input_schema": {"type": "object", "properties": {"service": {"type": "string"}}, "required": ["service"]},
        },
        {
            "name": "get_cost_breakdown",
            "description": "Break down this month's spend by service.",
            "input_schema": {"type": "object", "properties": {}},
        },
    ],
    indent=2,
)


# System prompts read as boilerplate until you actually see one; this is
# the second demo-candidate expand target (see scripts/demo_capture.py's
# --expand flag), included because it carries injected operational
# reminders (turn limits, redaction policy) most viewers wouldn't guess
# are sitting in every single request's context window, not just the
# first one.
_SYSTEM_PROMPT_CONTENT = (
    "You are an SRE assistant with read access to service health, deployment, "
    "and cost data via the tools below. Investigate before answering; do not "
    "guess at root cause.\n\n"
    "Constraints:\n"
    "- Never claim a fix is confirmed unless a tool result directly supports it.\n"
    "- If evidence is inconclusive, say so explicitly rather than picking the "
    "most likely story.\n"
    "- You have a hard limit of 15 turns per session; if you hit it, say what "
    "you found so far and mark the answer as partial.\n"
    "- Do not fabricate service names, metrics, or deployment IDs; only use "
    "what a tool call actually returned this session."
)

# The Context Window Explorer's third demo-candidate expand target: a
# real tool_result payload, so a viewer can see the actual JSON an
# answer was grounded in, not just a "225 tok" line item. Attached to
# the first ok tool call in a trace inside _context_blocks below.
_SAMPLE_TOOL_RESULT_CONTENT = json.dumps(
    {
        "service": "checkout-api",
        "p50_ms": 82,
        "p99_ms": 1240,
        "error_rate": 0.021,
        "window_minutes": 60,
        "note": "p99 rose sharply at 14:02, coincides with the last deploy",
    },
    indent=2,
)


def _context_blocks(turns, trace, answer_text, answer_label="Final answer"):
    blocks = [
        {
            "category": "system",
            "label": "System prompt",
            "char_count": 1400,
            "token_estimate": 350,
            "turn_n": None,
            "content": _SYSTEM_PROMPT_CONTENT,
        },
        {
            "category": "tools",
            "label": f"Tool specs ({len(_TOOLS)} tools)",
            "char_count": 2600,
            "token_estimate": 650,
            "turn_n": None,
            "content": _TOOL_SPECS_CONTENT,
        },
        {"category": "user", "label": "User prompt", "char_count": 80, "token_estimate": 20, "turn_n": 0},
    ]
    call_i = 0
    attached_sample_result = False
    for turn_n in range(turns):
        if turn_n > 0:
            blocks.append(
                {
                    "category": "reasoning",
                    "label": f"Reasoning (turn {turn_n})",
                    "char_count": 220,
                    "token_estimate": 55,
                    "turn_n": turn_n,
                }
            )
        if call_i < len(trace) and turn_n < turns - 1:
            call = trace[call_i]
            call_i += 1
            blocks.append(
                {
                    "category": "tool_call",
                    "label": f"Tool call: {call['tool']}",
                    "char_count": 60,
                    "token_estimate": 15,
                    "turn_n": turn_n,
                }
            )
            result_block = {
                "category": "tool_result",
                "label": f"Tool result: {call['tool']}",
                "char_count": 900 if call["status"] == "ok" else 90,
                "token_estimate": 225 if call["status"] == "ok" else 22,
                "turn_n": turn_n,
                "status": call["status"],
            }
            # Only the first successful tool_result per session carries
            # real content (see _SAMPLE_TOOL_RESULT_CONTENT above); every
            # other block deliberately keeps falling back to the "content
            # wasn't captured" placeholder, since real deployments won't
            # have raw bodies for most historical sessions either, and
            # the demo shouldn't misrepresent that as the common case.
            if not attached_sample_result and call["status"] == "ok":
                result_block["content"] = _SAMPLE_TOOL_RESULT_CONTENT
                attached_sample_result = True
            blocks.append(result_block)
    blocks.append(
        {
            "category": "answer",
            "label": answer_label,
            "char_count": len(answer_text),
            "token_estimate": max(1, round(len(answer_text) / 4)),
            "turn_n": turns - 1,
        }
    )
    return blocks


def _session(session_id, ts_offset, prompt, model_id, trace, turn_latencies, answer_text, hit_turn_limit=False):
    turns = [
        {
            "input_tokens": 900 + i * 40,
            "output_tokens": 60 + i * 10,
            "latency_ms": latency,
            "cache_read_input_tokens": 800 if i > 0 else 0,
            "cache_write_input_tokens": 900 if i == 0 else 0,
        }
        for i, latency in enumerate(turn_latencies)
    ]
    context_blocks = _context_blocks(
        len(turns),
        trace,
        answer_text,
        answer_label="Final answer (turn limit)" if hit_turn_limit else "Final answer",
    )
    return {
        "session_id": session_id,
        "ts": _BASE_TS + ts_offset,
        "prompt": prompt,
        "model_id": model_id,
        "turns": turns,
        "trace": trace,
        "context_blocks": context_blocks,
        "text": answer_text,
    }


def _trace(*pairs):
    """pairs like [("list_services", "ok"), ("search_logs", "error")]."""
    return [{"tool": tool, "args": {}, "status": status} for tool, status in pairs]


def build_sessions():
    sessions = []
    prompts = [
        ("why is checkout-api p99 latency elevated?", SONNET, _trace(("list_services", "ok"), ("get_service_metrics", "ok"), ("search_logs", "ok")), [1800, 2100, 1600], "checkout-api's p99 latency rose after the 14:02 deploy; logs show connection pool exhaustion against the payments-api dependency."),
        ("is payments-api healthy right now?", HAIKU, _trace(("get_service_metrics", "ok")), [900, 700], "payments-api is healthy: error rate 0.1%, p99 within baseline."),
        ("what deployed to auth-api in the last 24 hours?", SONNET, _trace(("get_recent_deployments", "ok")), [1200, 950], "One deployment to auth-api 6 hours ago (build a91f3c2), no rollback since."),
        ("compare error rates across all services this week", SONNET, _trace(("list_services", "ok"), ("get_service_metrics", "ok"), ("get_service_metrics", "ok"), ("get_service_metrics", "ok"), ("get_service_metrics", "ok")), [1900, 2200, 2000, 2100, 1700, 1300], "checkout-api and notifications both show elevated error rates this week; auth-api and payments-api are within baseline."),
        ("why did search_logs fail for checkout-api?", HAIKU, _trace(("search_logs", "error")), [800, 650], "search_logs failed: the log index for checkout-api's most recent window hadn't finished ingesting yet; retry after a minute."),
        ("is notifications healthy?", HAIKU, _trace(("get_service_metrics", "ok")), [750, 600], "notifications is healthy: no active incidents, latency and error rate both within baseline."),
        ("what's driving the cost increase this month?", SONNET, _trace(("get_cost_breakdown", "ok"), ("list_services", "ok")), [2000, 1600, 1400], "The increase is driven by checkout-api's higher request volume, not a pricing or efficiency regression."),
        ("compare checkout-api and payments-api tail latency", SONNET, _trace(("get_service_metrics", "ok"), ("get_service_metrics", "ok")), [1700, 1500, 1300], "checkout-api's p99 is roughly 3x payments-api's, consistent with the connection-pool issue found in an earlier investigation."),
        ("did the last checkout-api deploy cause the regression?", SONNET, _trace(("get_recent_deployments", "ok"), ("get_service_metrics", "ok"), ("search_logs", "error")), [1900, 2000, 1700, 1200], "Timing lines up with the 14:02 deploy, but search_logs couldn't confirm via logs, so treat this as likely, not certain."),
        ("list all services and their current status", HAIKU, _trace(("list_services", "ok")), [850], "5 services tracked: checkout-api, payments-api, auth-api, notifications, and search. All reporting except search, which returned no recent data."),
        ("what tools are available to this agent?", HAIKU, _trace(), [500], "5 tools: list_services, get_service_metrics, search_logs, get_recent_deployments, get_cost_breakdown."),
        (
            "do a full multi-signal investigation of the checkout-api incident",
            SONNET,
            _trace(
                ("list_services", "ok"),
                ("get_service_metrics", "ok"),
                ("get_recent_deployments", "ok"),
                ("search_logs", "ok"),
                ("search_logs", "ok"),
                ("get_cost_breakdown", "ok"),
                ("get_service_metrics", "ok"),
                ("search_logs", "error"),
                ("get_recent_deployments", "ok"),
                ("get_service_metrics", "ok"),
                ("search_logs", "ok"),
                ("get_cost_breakdown", "ok"),
                ("get_service_metrics", "ok"),
                ("search_logs", "ok"),
            ),
            [1800] * 15,
            "Hit the turn limit without finishing.",
            True,
        ),
    ]

    for i, entry in enumerate(prompts):
        prompt, model_id, trace, latencies, answer, *rest = entry
        hit_limit = rest[0] if rest else False
        sessions.append(
            _session(
                session_id=f"demo-session-{i + 1:02d}",
                ts_offset=i * 3600,
                prompt=prompt,
                model_id=model_id,
                trace=trace,
                turn_latencies=latencies,
                answer_text=answer,
                hit_turn_limit=hit_limit,
            )
        )
    return sessions


def seed(out_path: Path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    conn = sqlite3.connect(out_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)

    for s in build_sessions():
        input_tokens = sum(t["input_tokens"] for t in s["turns"])
        output_tokens = sum(t["output_tokens"] for t in s["turns"])
        total_tokens = input_tokens + output_tokens
        latency_ms = sum(t["latency_ms"] for t in s["turns"])
        cost = estimate_cost(s["model_id"], input_tokens, output_tokens)

        conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                s["session_id"],
                s["prompt"],
                s["model_id"],
                input_tokens,
                output_tokens,
                total_tokens,
                latency_ms,
                len(s["trace"]),
                cost,
                s["ts"],
                None,
                "bedrock_agent",
                "closed",
            ),
        )
        for i, turn in enumerate(s["turns"]):
            conn.execute(
                "INSERT INTO turns VALUES (?,?,?,?,?,?,?)",
                (
                    s["session_id"],
                    i,
                    turn["input_tokens"],
                    turn["output_tokens"],
                    turn["latency_ms"],
                    turn["cache_read_input_tokens"],
                    turn["cache_write_input_tokens"],
                ),
            )
        for i, call in enumerate(s["trace"]):
            conn.execute(
                "INSERT INTO tool_calls VALUES (?,?,?,?,?,?,?)",
                (s["session_id"], i, call["tool"], json.dumps(call["args"]), call["status"], 0, s["ts"]),
            )
        for seq, block in enumerate(s["context_blocks"]):
            conn.execute(
                "INSERT INTO context_blocks VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    s["session_id"],
                    seq,
                    block["category"],
                    block["label"],
                    block["char_count"],
                    block["token_estimate"],
                    block["turn_n"],
                    block.get("status"),
                    block.get("content"),
                ),
            )
    conn.commit()
    conn.close()
    return out_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    path = seed(args.out)
    print(f"Wrote {len(build_sessions())} demo sessions to {path}")


if __name__ == "__main__":
    main()
