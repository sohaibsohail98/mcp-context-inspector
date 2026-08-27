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


def _context_blocks(turns, trace, answer_text, answer_label="Final answer"):
    blocks = [
        {"category": "system", "label": "System prompt", "char_count": 1400, "token_estimate": 350, "turn_n": None},
        {
            "category": "tools",
            "label": f"Tool specs ({len(_TOOLS)} tools)",
            "char_count": 2600,
            "token_estimate": 650,
            "turn_n": None,
        },
        {"category": "user", "label": "User prompt", "char_count": 80, "token_estimate": 20, "turn_n": 0},
    ]
    call_i = 0
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
            blocks.append(
                {
                    "category": "tool_result",
                    "label": f"Tool result: {call['tool']}",
                    "char_count": 900 if call["status"] == "ok" else 90,
                    "token_estimate": 225 if call["status"] == "ok" else 22,
                    "turn_n": turn_n,
                    "status": call["status"],
                }
            )
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
