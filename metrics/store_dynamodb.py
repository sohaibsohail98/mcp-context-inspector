"""DynamoDB backend for the execution recorder — swapped in at Phase 5
deployment per docs/PROJECT.md's Storage section (AgentCore Runtime's
container filesystem doesn't persist across sessions, so SQLite silently
loses data once deployed). Same function signatures as store_sqlite.py;
callers (app.py, mcp_server/server.py) go through store.py's dispatcher
and never know which backend is active.

Single-table design: partition key session_id, sort key sk distinguishes
item type ("SESSION", "TURN#0000", "TOOLCALL#0000", ...). Aggregate reads
(recent sessions, aggregate tool metrics) use Scan — fine at the personal
project scale this is built for; revisit with a GSI if that ever stops
being true.
"""

import json
import os
import time
import uuid
from decimal import Decimal

import boto3

from mci_common.config import CONTEXT_WINDOW_TOKENS, DEFAULT_REGION
from mci_common.dynamo import clean_decimal as _clean
from mci_common.pricing import estimate_cost

TABLE_NAME = os.environ.get("METRICS_TABLE", "sre-agent-metrics")
REGION = os.environ.get("AWS_REGION", DEFAULT_REGION)

_table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME)


def record_session(prompt, model_id, loop_result):
    session_id = str(uuid.uuid4())
    cost = estimate_cost(model_id, loop_result["input_tokens"], loop_result["output_tokens"])
    ts = time.time()

    with _table.batch_writer() as batch:
        batch.put_item(
            Item={
                "session_id": session_id,
                "sk": "SESSION",
                "prompt": prompt,
                "model": model_id,
                "input_tokens": loop_result["input_tokens"],
                "output_tokens": loop_result["output_tokens"],
                "total_tokens": loop_result["total_tokens"],
                "latency_ms": loop_result["latency_ms"],
                "tool_call_count": len(loop_result["trace"]),
                "estimated_cost": Decimal(str(cost)),
                "timestamp": Decimal(str(ts)),
            }
        )
        for i, turn in enumerate(loop_result["turns"]):
            batch.put_item(
                Item={
                    "session_id": session_id,
                    "sk": f"TURN#{i:04d}",
                    "turn_n": i,
                    "input_tokens": turn["input_tokens"],
                    "output_tokens": turn["output_tokens"],
                    "latency_ms": turn["latency_ms"],
                    "cache_read_input_tokens": turn.get("cache_read_input_tokens", 0),
                    "cache_write_input_tokens": turn.get("cache_write_input_tokens", 0),
                }
            )
        for i, call in enumerate(loop_result["trace"]):
            batch.put_item(
                Item={
                    "session_id": session_id,
                    "sk": f"TOOLCALL#{i:04d}",
                    "tool_name": call["tool"],
                    "args": json.dumps(call["args"]),
                    "status": call["status"],
                }
            )
        for i, block in enumerate(loop_result.get("context_blocks", [])):
            item = {
                "session_id": session_id,
                "sk": f"CTXBLOCK#{i:04d}",
                "category": block["category"],
                "label": block["label"],
                "char_count": block["char_count"],
                "token_estimate": block["token_estimate"],
                "turn_n": block["turn_n"],
            }
            if block.get("status") is not None:
                item["status"] = block["status"]
            batch.put_item(Item=item)
    return session_id


def _scan_all(**kwargs):
    """Scan with pagination — a single Scan call stops at 1MB *scanned*
    (before any FilterExpression), silently returning a partial result if
    more remains. Aggregate reads (recent sessions, aggregate tool
    metrics, aggregate cost) need every item, not just the first page.
    """
    items = []
    while True:
        resp = _table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            return items
        kwargs["ExclusiveStartKey"] = last_key


def get_session_metrics(session_id):
    """Session metadata split from per-prompt processing metrics — same
    shape contract as store_sqlite.py's version (see that docstring)."""
    resp = _table.get_item(Key={"session_id": session_id, "sk": "SESSION"})
    item = resp.get("Item")
    if not item:
        return None
    item = _clean(item)
    return {
        "session": {
            "session_id": item["session_id"],
            "model": item["model"],
            "timestamp": item["timestamp"],
        },
        "prompt_metrics": {
            "prompt": item["prompt"],
            "input_tokens": item["input_tokens"],
            "output_tokens": item["output_tokens"],
            "total_tokens": item["total_tokens"],
            "latency_ms": item["latency_ms"],
            "tool_call_count": item["tool_call_count"],
            "estimated_cost": item["estimated_cost"],
        },
    }


def get_token_breakdown(session_id):
    resp = _table.query(
        KeyConditionExpression="session_id = :sid AND begins_with(sk, :prefix)",
        ExpressionAttributeValues={":sid": session_id, ":prefix": "TURN#"},
    )
    items = sorted(resp.get("Items", []), key=lambda i: i["sk"])
    return [
        {
            "turn_n": i["turn_n"],
            "input_tokens": i["input_tokens"],
            "output_tokens": i["output_tokens"],
            "latency_ms": i["latency_ms"],
            "cache_read_input_tokens": i.get("cache_read_input_tokens", 0),
            "cache_write_input_tokens": i.get("cache_write_input_tokens", 0),
        }
        for i in _clean(items)
    ]


def get_agent_trace(session_id):
    resp = _table.query(
        KeyConditionExpression="session_id = :sid AND begins_with(sk, :prefix)",
        ExpressionAttributeValues={":sid": session_id, ":prefix": "TOOLCALL#"},
    )
    items = sorted(resp.get("Items", []), key=lambda i: i["sk"])
    return [
        {"tool": i["tool_name"], "args": json.loads(i["args"]), "status": i["status"]}
        for i in items
    ]


def get_context_timeline(session_id):
    """Same contract as store_sqlite.py's version — see that docstring."""
    resp = _table.query(
        KeyConditionExpression="session_id = :sid AND begins_with(sk, :prefix)",
        ExpressionAttributeValues={":sid": session_id, ":prefix": "CTXBLOCK#"},
    )
    items = sorted(_clean(resp.get("Items", [])), key=lambda i: i["sk"])
    blocks = []
    cumulative_tokens = 0
    for i in items:
        cumulative_tokens += i["token_estimate"]
        blocks.append(
            {
                "category": i["category"],
                "label": i["label"],
                "char_count": i["char_count"],
                "token_estimate": i["token_estimate"],
                "turn_n": i["turn_n"],
                "status": i.get("status"),
                "cumulative_tokens": cumulative_tokens,
                "cumulative_pct": round(cumulative_tokens / CONTEXT_WINDOW_TOKENS * 100, 4),
            }
        )
    return blocks


def get_tool_metrics(session_id=None):
    if session_id:
        resp = _table.query(
            KeyConditionExpression="session_id = :sid AND begins_with(sk, :prefix)",
            ExpressionAttributeValues={":sid": session_id, ":prefix": "TOOLCALL#"},
        )
        items = resp.get("Items", [])
    else:
        items = _scan_all(
            FilterExpression="begins_with(sk, :prefix)",
            ExpressionAttributeValues={":prefix": "TOOLCALL#"},
        )

    counts = {}
    for i in items:
        key = (i["tool_name"], i["status"])
        counts[key] = counts.get(key, 0) + 1
    return [{"tool_name": t, "status": s, "calls": c} for (t, s), c in counts.items()]


def get_cost_estimate(session_id=None, period_seconds=None):
    if session_id:
        item = get_session_metrics(session_id)
        return item["prompt_metrics"]["estimated_cost"] if item else None

    items = _clean(
        _scan_all(FilterExpression="sk = :sk", ExpressionAttributeValues={":sk": "SESSION"})
    )
    since = time.time() - period_seconds if period_seconds else 0
    return sum(i["estimated_cost"] for i in items if i["timestamp"] >= since)


def get_recent_sessions(limit=10):
    items = _clean(
        _scan_all(FilterExpression="sk = :sk", ExpressionAttributeValues={":sk": "SESSION"})
    )
    items.sort(key=lambda i: i["timestamp"], reverse=True)
    return [
        {
            "session_id": i["session_id"],
            "prompt": i["prompt"],
            "model": i["model"],
            "total_tokens": i["total_tokens"],
            "estimated_cost": i["estimated_cost"],
            "timestamp": i["timestamp"],
        }
        for i in items[:limit]
    ]
