"""DynamoDB backend for the execution recorder, used once deployed,
since a container's local filesystem doesn't persist across
invocations and SQLite would silently lose data. Same function
signatures as store_sqlite.py; callers (app.py, mcp_server/server.py)
go through store.py's dispatcher and never know which backend is
active.

Single-table design: partition key session_id, sort key sk distinguishes
item type ("SESSION", "TURN#0000", "TOOLCALL#0000", ...). Aggregate reads
(recent sessions, aggregate tool metrics) use Scan. Fine at personal
project scale; revisit with a GSI if that ever stops being true.
"""

import json
import os
import time
import uuid
from decimal import Decimal

import boto3

from mci_common.config import DEFAULT_REGION
from mci_common.dynamo import clean_decimal as _clean
from mci_common.pricing import estimate_cost
from mci_common.timeline import build_timeline
from metrics.errors import SessionOwnershipError

TABLE_NAME = os.environ.get("METRICS_TABLE", "sre-agent-metrics")
REGION = os.environ.get("AWS_REGION", DEFAULT_REGION)

_table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME)


def _visible(session_owner, caller_owner):
    """caller_owner=None means "the admin/owner token", which sees
    everything. Otherwise a caller only sees sessions it owns. Same
    contract as store_sqlite.py's version."""
    return caller_owner is None or session_owner == caller_owner


def _session_owner(session_id):
    resp = _table.get_item(Key={"session_id": session_id, "sk": "SESSION"})
    item = resp.get("Item")
    return item.get("owner") if item else None


def record_session(prompt, model_id, loop_result, owner=None):
    """owner is the Google account `sub` that recorded this session, or
    None for the server owner's own; see _visible(). This stays the
    one-shot atomic path (source='bedrock_agent', status 'closed'
    immediately). OTLP ingestion uses start_or_get_session + append_*
    below instead, since that data arrives incrementally."""
    session_id = str(uuid.uuid4())
    cost = estimate_cost(model_id, loop_result["input_tokens"], loop_result["output_tokens"])
    ts = time.time()

    with _table.batch_writer() as batch:
        session_item = {
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
            "source": "bedrock_agent",
            "status": "closed",
        }
        if owner is not None:
            session_item["owner"] = owner
        batch.put_item(Item=session_item)
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
                    "latency_ms": call.get("latency_ms", 0),
                    "timestamp": Decimal(str(call.get("timestamp", ts))),
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
            if block.get("content") is not None:
                item["content"] = block["content"]
            batch.put_item(Item=item)
    return session_id


def _scan_all(**kwargs):
    """Scan with pagination. A single Scan call stops at 1MB *scanned*
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


def get_session_metrics(session_id, owner=None):
    """Session metadata split from per-prompt processing metrics, same
    shape contract as store_sqlite.py's version (see that docstring,
    including the owner-filtering behavior)."""
    resp = _table.get_item(Key={"session_id": session_id, "sk": "SESSION"})
    item = resp.get("Item")
    if not item or not _visible(item.get("owner"), owner):
        return None
    item = _clean(item)
    return {
        "session": {
            "session_id": item["session_id"],
            "model": item["model"],
            "timestamp": item["timestamp"],
            "source": item.get("source", "bedrock_agent"),
            "status": item.get("status", "closed"),
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


def get_token_breakdown(session_id, owner=None):
    if not _visible(_session_owner(session_id), owner):
        return []
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


def get_agent_trace(session_id, owner=None):
    if not _visible(_session_owner(session_id), owner):
        return []
    resp = _table.query(
        KeyConditionExpression="session_id = :sid AND begins_with(sk, :prefix)",
        ExpressionAttributeValues={":sid": session_id, ":prefix": "TOOLCALL#"},
    )
    items = sorted(_clean(resp.get("Items", [])), key=lambda i: i["sk"])
    return [
        {
            "tool": i["tool_name"],
            "args": json.loads(i["args"]),
            "status": i["status"],
            "latency_ms": i.get("latency_ms", 0),
            "timestamp": i.get("timestamp", 0),
        }
        for i in items
    ]


def get_context_timeline(session_id, owner=None):
    """Same contract as store_sqlite.py's version; see that docstring."""
    if not _visible(_session_owner(session_id), owner):
        return []
    resp = _table.query(
        KeyConditionExpression="session_id = :sid AND begins_with(sk, :prefix)",
        ExpressionAttributeValues={":sid": session_id, ":prefix": "CTXBLOCK#"},
    )
    items = sorted(_clean(resp.get("Items", [])), key=lambda i: i["sk"])
    rows = (
        {
            "category": i["category"],
            "label": i["label"],
            "char_count": i["char_count"],
            "token_estimate": i["token_estimate"],
            "turn_n": i["turn_n"],
            "status": i.get("status"),
            "content": i.get("content"),
        }
        for i in items
    )
    return build_timeline(rows)


def _owned_session_ids(owner):
    """Every session_id belonging to `owner`, used to filter aggregate
    scans over TOOLCALL#/etc. items, which don't carry `owner`
    themselves (only the SESSION item does). A second Scan, not a JOIN
    (DynamoDB has none). Fine at this project's personal scale, same
    reasoning as _scan_all's own docstring."""
    items = _scan_all(
        FilterExpression="sk = :sk AND #o = :owner",
        ExpressionAttributeNames={"#o": "owner"},
        ExpressionAttributeValues={":sk": "SESSION", ":owner": owner},
    )
    return {i["session_id"] for i in items}


def get_tool_metrics(session_id=None, owner=None):
    if session_id:
        if not _visible(_session_owner(session_id), owner):
            return []
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
        if owner is not None:
            owned = _owned_session_ids(owner)
            items = [i for i in items if i["session_id"] in owned]

    counts = {}
    for i in items:
        key = (i["tool_name"], i["status"])
        counts[key] = counts.get(key, 0) + 1
    return [{"tool_name": t, "status": s, "calls": c} for (t, s), c in counts.items()]


def get_cost_estimate(session_id=None, period_seconds=None, owner=None):
    if session_id:
        item = get_session_metrics(session_id, owner=owner)
        return item["prompt_metrics"]["estimated_cost"] if item else None

    if owner is not None:
        items = _clean(
            _scan_all(
                FilterExpression="sk = :sk AND #o = :owner",
                ExpressionAttributeNames={"#o": "owner"},
                ExpressionAttributeValues={":sk": "SESSION", ":owner": owner},
            )
        )
    else:
        items = _clean(
            _scan_all(FilterExpression="sk = :sk", ExpressionAttributeValues={":sk": "SESSION"})
        )
    since = time.time() - period_seconds if period_seconds else 0
    return sum(i["estimated_cost"] for i in items if i["timestamp"] >= since)


def get_recent_sessions(limit=10, owner=None, include_test_sessions=False):
    """include_test_sessions=False (the default) drops rows whose
    session_id starts with "api-tests-"; see store_sqlite.py's
    get_recent_sessions docstring."""
    if owner is not None:
        items = _clean(
            _scan_all(
                FilterExpression="sk = :sk AND #o = :owner",
                ExpressionAttributeNames={"#o": "owner"},
                ExpressionAttributeValues={":sk": "SESSION", ":owner": owner},
            )
        )
    else:
        items = _clean(
            _scan_all(FilterExpression="sk = :sk", ExpressionAttributeValues={":sk": "SESSION"})
        )

    if not include_test_sessions:
        items = [i for i in items if not i["session_id"].startswith("api-tests-")]
    items.sort(key=lambda i: i["timestamp"], reverse=True)
    # Turn/tool-call aggregates via bounded Queries per already-sliced-to-
    # `limit` session (partition-key lookup, same pattern
    # get_token_breakdown uses), not a full-table Scan per row, and
    # bounded by `limit` (the dashboard's page size), not the whole table.
    result = []
    for i in items[:limit]:
        turns = _clean(
            _table.query(
                KeyConditionExpression="session_id = :sid AND begins_with(sk, :prefix)",
                ExpressionAttributeValues={":sid": i["session_id"], ":prefix": "TURN#"},
            )["Items"]
        )
        tool_calls = _clean(
            _table.query(
                KeyConditionExpression="session_id = :sid AND begins_with(sk, :prefix)",
                ExpressionAttributeValues={":sid": i["session_id"], ":prefix": "TOOLCALL#"},
            )["Items"]
        )
        result.append(
            {
                "session_id": i["session_id"],
                "prompt": i["prompt"],
                "model": i["model"],
                "total_tokens": i["total_tokens"],
                "estimated_cost": i["estimated_cost"],
                "timestamp": i["timestamp"],
                "source": i.get("source", "bedrock_agent"),
                "status": i.get("status", "closed"),
                "turn_count": len(turns),
                "cache_read_tokens": sum(t.get("cache_read_input_tokens", 0) for t in turns),
                "fresh_input_tokens": sum(t.get("input_tokens", 0) for t in turns),
                "tool_call_total": len(tool_calls),
                "tool_call_errors": sum(1 for c in tool_calls if c.get("status") == "error"),
            }
        )
    return result


def start_or_get_session(session_id, owner=None, source="claude_code", model=None):
    """Entry point for OTLP ingestion; see store_sqlite.py's docstring
    for the full reasoning; same contract here. Conditional put (not a
    plain get-then-put) closes the race where two OTLP batches for a
    brand-new session_id arrive close together. DynamoDB rejects the
    second write instead of both racing to "create" the same session."""
    existing = _table.get_item(Key={"session_id": session_id, "sk": "SESSION"}).get("Item")
    if existing:
        if not _visible(existing.get("owner"), owner):
            raise SessionOwnershipError(f"session_id {session_id!r} belongs to a different owner")
        return session_id
    item = {
        "session_id": session_id,
        "sk": "SESSION",
        "prompt": None,
        "model": model,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "latency_ms": 0,
        "tool_call_count": 0,
        "estimated_cost": Decimal("0"),
        "timestamp": Decimal(str(time.time())),
        "source": source,
        "status": "open",
    }
    if owner is not None:
        item["owner"] = owner
    try:
        _table.put_item(Item=item, ConditionExpression="attribute_not_exists(session_id)")
    except _table.meta.client.exceptions.ConditionalCheckFailedException:
        pass
    return session_id


def _next_index(session_id, sk_prefix):
    resp = _table.query(
        KeyConditionExpression="session_id = :sid AND begins_with(sk, :prefix)",
        ExpressionAttributeValues={":sid": session_id, ":prefix": sk_prefix},
        Select="COUNT",
    )
    return resp["Count"]


# Two concurrent OTLP batches for the same session can both call
# _next_index and get the same count back, then both put_item the same
# sk. A plain put_item would let the second silently overwrite the
# first. attribute_not_exists(sk) rejects the second writer instead;
# _put_next_indexed retries with a freshly recomputed index until a
# write succeeds. A cap bounds retries against a determined write storm
# on one session rather than spinning forever.
_MAX_INDEX_RETRIES = 10


def _put_next_indexed(session_id, sk_prefix, build_item):
    """build_item(index) -> the full Item dict to put, sk included.
    Returns the index that was actually written."""
    for _ in range(_MAX_INDEX_RETRIES):
        index = _next_index(session_id, sk_prefix)
        try:
            _table.put_item(
                Item=build_item(index),
                ConditionExpression="attribute_not_exists(sk)",
            )
            return index
        except _table.meta.client.exceptions.ConditionalCheckFailedException:
            continue
    raise RuntimeError(
        f"could not allocate a unique {sk_prefix!r} index for session_id {session_id!r} "
        f"after {_MAX_INDEX_RETRIES} attempts"
    )


def _check_ownership_or_raise(session_id, owner):
    """Same reasoning as store_sqlite.py's version: every append_*/
    close_session call must not be able to write into a session_id
    owned by someone else. A missing session (append called before
    start_or_get_session) is treated as belonging to no one, i.e.
    deny-by-default for any non-admin caller."""
    if not _visible(_session_owner(session_id), owner):
        raise SessionOwnershipError(f"session_id {session_id!r} belongs to a different owner")


def _recost_session(session_id):
    """Re-derive estimated_cost from the session's own model + running
    token totals, same reasoning as store_sqlite.py's version."""
    item = _table.get_item(Key={"session_id": session_id, "sk": "SESSION"}).get("Item")
    if not item:
        return
    item = _clean(item)
    cost = estimate_cost(item["model"], item["input_tokens"], item["output_tokens"])
    _table.update_item(
        Key={"session_id": session_id, "sk": "SESSION"},
        UpdateExpression="SET estimated_cost = :c",
        ExpressionAttributeValues={":c": Decimal(str(cost))},
    )


def append_turn(session_id, turn_data, owner=None):
    """turn_data: {input_tokens, output_tokens, latency_ms,
    cache_read_input_tokens=0, cache_write_input_tokens=0}. Same
    contract as store_sqlite.py's version, including the owner check."""
    _check_ownership_or_raise(session_id, owner)
    _put_next_indexed(
        session_id,
        "TURN#",
        lambda turn_n: {
            "session_id": session_id,
            "sk": f"TURN#{turn_n:04d}",
            "turn_n": turn_n,
            "input_tokens": turn_data["input_tokens"],
            "output_tokens": turn_data["output_tokens"],
            "latency_ms": turn_data["latency_ms"],
            "cache_read_input_tokens": turn_data.get("cache_read_input_tokens", 0),
            "cache_write_input_tokens": turn_data.get("cache_write_input_tokens", 0),
        },
    )
    _table.update_item(
        Key={"session_id": session_id, "sk": "SESSION"},
        UpdateExpression=(
            "SET input_tokens = input_tokens + :i, output_tokens = output_tokens + :o, "
            "total_tokens = total_tokens + :t, latency_ms = latency_ms + :l"
        ),
        ExpressionAttributeValues={
            ":i": turn_data["input_tokens"],
            ":o": turn_data["output_tokens"],
            ":t": turn_data["input_tokens"] + turn_data["output_tokens"],
            ":l": turn_data["latency_ms"],
        },
    )
    _recost_session(session_id)


def append_tool_call(session_id, tool_call, owner=None):
    """tool_call: {tool, args, status, latency_ms=0, timestamp=None
    (defaults to now)}. owner must match the session's own owner."""
    _check_ownership_or_raise(session_id, owner)
    _put_next_indexed(
        session_id,
        "TOOLCALL#",
        lambda seq: {
            "session_id": session_id,
            "sk": f"TOOLCALL#{seq:04d}",
            "tool_name": tool_call["tool"],
            "args": json.dumps(tool_call["args"]),
            "status": tool_call["status"],
            "latency_ms": tool_call.get("latency_ms", 0),
            "timestamp": Decimal(str(tool_call.get("timestamp") or time.time())),
        },
    )
    _table.update_item(
        Key={"session_id": session_id, "sk": "SESSION"},
        UpdateExpression="SET tool_call_count = tool_call_count + :one",
        ExpressionAttributeValues={":one": 1},
    )


_PROMPT_PREVIEW_CHARS = 200


def append_context_block(session_id, block, owner=None):
    """block: {category, label, char_count, token_estimate, turn_n,
    status=None, content=None}, the same shape record_session's
    context_blocks rows use. owner must match the session's own owner.

    Same backfill reasoning as store_sqlite.py's version: OTLP sessions
    are created by start_or_get_session with prompt=None, since a live
    session has no single upfront prompt argument the way record_session
    does. The first real user-authored block (category == "user")
    backfills the SESSION item's prompt attribute here, once. Re-fetch
    the item and check its current prompt before writing, so a later
    turn's user message can never overwrite turn 0's original prompt.
    Truncated for list-view display; the full text already lives in this
    same block's own CTXBLOCK# item, so this would otherwise duplicate
    it."""
    _check_ownership_or_raise(session_id, owner)

    def build_item(seq):
        item = {
            "session_id": session_id,
            "sk": f"CTXBLOCK#{seq:04d}",
            "category": block["category"],
            "label": block["label"],
            "char_count": block["char_count"],
            "token_estimate": block["token_estimate"],
            "turn_n": block["turn_n"],
        }
        if block.get("status") is not None:
            item["status"] = block["status"]
        if block.get("content") is not None:
            item["content"] = block["content"]
        return item

    _put_next_indexed(session_id, "CTXBLOCK#", build_item)

    if block["category"] == "user":
        session_item = _table.get_item(Key={"session_id": session_id, "sk": "SESSION"}).get("Item")
        if session_item and not session_item.get("prompt"):
            preview = (block.get("content") or block.get("label") or "")[:_PROMPT_PREVIEW_CHARS]
            _table.update_item(
                Key={"session_id": session_id, "sk": "SESSION"},
                UpdateExpression="SET prompt = :p",
                ExpressionAttributeValues={":p": preview},
            )


def close_session(session_id, final_totals=None, owner=None):
    """Same contract as store_sqlite.py's version; see that docstring,
    including the owner check."""
    _check_ownership_or_raise(session_id, owner)
    if final_totals:
        _table.update_item(
            Key={"session_id": session_id, "sk": "SESSION"},
            UpdateExpression=(
                "SET input_tokens = :i, output_tokens = :o, total_tokens = :t, latency_ms = :l"
            ),
            ExpressionAttributeValues={
                ":i": final_totals["input_tokens"],
                ":o": final_totals["output_tokens"],
                ":t": final_totals["total_tokens"],
                ":l": final_totals["latency_ms"],
            },
        )
        _recost_session(session_id)
    _table.update_item(
        Key={"session_id": session_id, "sk": "SESSION"},
        UpdateExpression="SET #s = :closed",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":closed": "closed"},
    )
