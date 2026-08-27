"""Regression tests for metrics/store_dynamodb.py, using a minimal
in-memory fake table, not moto and not real AWS. The fake only implements
the exact scan/query/get_item/batch_writer calls store_dynamodb.py
actually issues; it is not a general DynamoDB emulator.

Covers two correctness contracts:
1. get_session_metrics must not leak the internal "sk" partition-key
   field, whose shape must match store_sqlite.py's version of the same
   function (the two are meant to be interchangeable behind
   metrics/store.py's dispatcher).
2. Aggregate reads (get_recent_sessions, get_tool_metrics, get_cost_estimate)
   must paginate: a single unpaginated Scan call truncates once it
   hits DynamoDB's per-call scan limit, silently undercounting/omitting
   recent items as the table grows, with no error raised.
"""

from decimal import Decimal

import pytest

from metrics import store_dynamodb


class _ConditionalCheckFailedException(Exception):
    """Stand-in for boto3's ClientError subclass DynamoDB raises when a
    ConditionExpression fails. Real boto3 raises a dynamically-generated
    exception class off Table.meta.client.exceptions, so this fake wires up
    just enough of that attribute chain (meta.client.exceptions.<Name>)
    for start_or_get_session's `except ...ConditionalCheckFailedException`
    to catch it."""


class _FakeExceptions:
    ConditionalCheckFailedException = _ConditionalCheckFailedException


class _FakeClient:
    exceptions = _FakeExceptions()


class _FakeMeta:
    client = _FakeClient()


class _FakeBatchWriter:
    def __init__(self, table):
        self.table = table

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def put_item(self, Item):
        self.table.items.append(Item)


class FakeTable:
    """Mimics exactly the boto3 Table calls store_dynamodb.py makes.
    page_size lets tests force multi-page Scan results, the thing the
    real bug depended on."""

    def __init__(self, items=None, page_size=1000):
        self.items = items or []
        self.page_size = page_size
        self.meta = _FakeMeta()

    def _apply_filter(self, expr, values, names=None):
        if expr is None:
            return list(self.items)
        if expr.strip() == "sk = :sk AND #o = :owner":
            owner_attr = (names or {})["#o"]
            return [i for i in self.items if i["sk"] == values[":sk"] and i.get(owner_attr) == values[":owner"]]
        if expr.startswith("begins_with"):
            prefix = values[":prefix"]
            return [i for i in self.items if i["sk"].startswith(prefix)]
        if expr.strip().startswith("sk ="):
            val = values[":sk"]
            return [i for i in self.items if i["sk"] == val]
        raise NotImplementedError(expr)

    def scan(
        self,
        FilterExpression=None,
        ExpressionAttributeValues=None,
        ExpressionAttributeNames=None,
        ExclusiveStartKey=None,
        **_,
    ):
        filtered = self._apply_filter(FilterExpression, ExpressionAttributeValues, ExpressionAttributeNames)
        start = ExclusiveStartKey or 0
        page = filtered[start : start + self.page_size]
        resp = {"Items": page}
        next_start = start + self.page_size
        if next_start < len(filtered):
            resp["LastEvaluatedKey"] = next_start
        return resp

    def query(self, KeyConditionExpression=None, ExpressionAttributeValues=None, Select=None, **_):
        sid = ExpressionAttributeValues[":sid"]
        prefix = ExpressionAttributeValues.get(":prefix")
        items = [i for i in self.items if i["session_id"] == sid and (prefix is None or i["sk"].startswith(prefix))]
        resp = {"Items": items}
        if Select == "COUNT":
            resp["Count"] = len(items)
        return resp

    def get_item(self, Key):
        for i in self.items:
            if i["session_id"] == Key["session_id"] and i["sk"] == Key["sk"]:
                return {"Item": i}
        return {}

    def put_item(self, Item, ConditionExpression=None):
        """Handles the two ConditionExpressions store_dynamodb.py issues
        ("attribute_not_exists(session_id)" from start_or_get_session,
        "attribute_not_exists(sk)" from _put_next_indexed), raises the
        fake ConditionalCheckFailedException if an item with the same
        session_id+sk already exists, otherwise upserts."""
        existing_idx = next(
            (
                idx
                for idx, i in enumerate(self.items)
                if i["session_id"] == Item["session_id"] and i["sk"] == Item["sk"]
            ),
            None,
        )
        if existing_idx is not None:
            if ConditionExpression in ("attribute_not_exists(session_id)", "attribute_not_exists(sk)"):
                raise self.meta.client.exceptions.ConditionalCheckFailedException()
            self.items[existing_idx] = Item
        else:
            self.items.append(Item)
        return {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None, ExpressionAttributeNames=None):
        """Handles exactly the UpdateExpression shapes store_dynamodb.py
        issues: comma-separated `field = field + :val` (increment) or
        `field = :val` / `#alias = :val` (overwrite) clauses, not a
        general expression parser."""
        values = ExpressionAttributeValues or {}
        names = ExpressionAttributeNames or {}
        item = next(
            (i for i in self.items if i["session_id"] == Key["session_id"] and i["sk"] == Key["sk"]),
            None,
        )
        if item is None:
            raise KeyError(f"FakeTable.update_item: no item for key {Key}")
        expr = UpdateExpression.strip()
        assert expr.startswith("SET "), f"unsupported UpdateExpression: {expr}"
        for clause in expr[len("SET ") :].split(","):
            field, rhs = clause.strip().split("=", 1)
            field = field.strip()
            rhs = rhs.strip()
            if field.startswith("#"):
                field = names[field]
            if "+" in rhs:
                _, val_key = rhs.split("+", 1)
                item[field] = item.get(field, 0) + values[val_key.strip()]
            else:
                item[field] = values[rhs.strip()]
        return {}

    def batch_writer(self):
        return _FakeBatchWriter(self)


@pytest.fixture
def fake_table(monkeypatch):
    table = FakeTable()
    monkeypatch.setattr(store_dynamodb, "_table", table)
    return table


def _session_item(session_id, **overrides):
    item = {
        "session_id": session_id,
        "sk": "SESSION",
        "prompt": "why is x degraded?",
        "model": "us.anthropic.claude-sonnet-4-6",
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "latency_ms": 500,
        "tool_call_count": 1,
        "estimated_cost": Decimal("0.001"),
        "timestamp": Decimal("1000000000"),
    }
    item.update(overrides)
    return item


def test_get_session_metrics_strips_internal_partition_field(fake_table):
    fake_table.items.append(_session_item("s1"))
    metrics = store_dynamodb.get_session_metrics("s1")
    assert "sk" not in metrics["session"]
    assert "sk" not in metrics["prompt_metrics"]
    # Same session/prompt_metrics split the SQLite backend returns. This
    # is the interchangeability contract.
    assert set(metrics.keys()) == {"session", "prompt_metrics"}
    assert set(metrics["session"].keys()) == {"session_id", "model", "timestamp", "source", "status"}
    assert set(metrics["prompt_metrics"].keys()) == {
        "prompt",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "latency_ms",
        "tool_call_count",
        "estimated_cost",
    }


def test_get_session_metrics_missing_returns_none(fake_table):
    assert store_dynamodb.get_session_metrics("nope") is None


def test_scan_all_paginates_past_a_single_page(fake_table):
    """Force a table where results span 3 pages and confirm every item
    still comes back, not just the first page."""
    fake_table.page_size = 2
    for i in range(5):
        fake_table.items.append(
            {"session_id": "s1", "sk": f"TOOLCALL#{i:04d}", "tool_name": "search_logs", "status": "ok"}
        )
    result = store_dynamodb._scan_all(
        FilterExpression="begins_with(sk, :prefix)",
        ExpressionAttributeValues={":prefix": "TOOLCALL#"},
    )
    assert len(result) == 5


def test_get_recent_sessions_correct_across_pages(fake_table):
    fake_table.page_size = 1  # force pagination with only 3 sessions
    for i in range(3):
        fake_table.items.append(_session_item(f"s{i}", timestamp=Decimal(str(1000 + i))))

    recent = store_dynamodb.get_recent_sessions(limit=10)
    assert len(recent) == 3
    # Newest (highest timestamp) first. Would silently drop s2 if
    # pagination were broken, since it's on the last page.
    assert recent[0]["session_id"] == "s2"


def test_get_recent_sessions_carries_cache_and_tool_error_aggregates(fake_table):
    """KPI strip needs cache-hit-rate/tool-error-rate inputs in the same
    bulk fetch as everything else, matching store_sqlite.py's contract."""
    fake_table.items.append(_session_item("s1", timestamp=Decimal("1000")))
    fake_table.items.append(
        {"session_id": "s1", "sk": "TURN#0000", "cache_read_input_tokens": 300, "input_tokens": 100}
    )
    fake_table.items.append({"session_id": "s1", "sk": "TURN#0001", "cache_read_input_tokens": 0, "input_tokens": 50})
    fake_table.items.append({"session_id": "s1", "sk": "TOOLCALL#0000", "tool_name": "a", "status": "success"})
    fake_table.items.append({"session_id": "s1", "sk": "TOOLCALL#0001", "tool_name": "b", "status": "error"})
    fake_table.items.append({"session_id": "s1", "sk": "TOOLCALL#0002", "tool_name": "c", "status": "error"})

    recent = store_dynamodb.get_recent_sessions(limit=10)
    row = recent[0]
    assert row["cache_read_tokens"] == 300
    assert row["fresh_input_tokens"] == 150
    assert row["tool_call_total"] == 3
    assert row["tool_call_errors"] == 2


def test_get_tool_metrics_aggregate_across_pages(fake_table):
    fake_table.page_size = 1
    for i in range(4):
        fake_table.items.append(
            {"session_id": "s1", "sk": f"TOOLCALL#{i:04d}", "tool_name": "search_logs", "status": "ok"}
        )
    metrics = store_dynamodb.get_tool_metrics()
    assert metrics == [{"tool_name": "search_logs", "status": "ok", "calls": 4}]


def test_clean_converts_decimal_to_int_or_float():
    assert store_dynamodb._clean(Decimal("5")) == 5
    assert isinstance(store_dynamodb._clean(Decimal("5")), int)
    assert store_dynamodb._clean(Decimal("5.5")) == 5.5
    assert isinstance(store_dynamodb._clean(Decimal("5.5")), float)


def test_get_token_breakdown_includes_cache_fields_with_default(fake_table):
    """Prompt-caching fields (§6a) default to 0 for TURN# items written
    before caching existed, rather than KeyError-ing."""
    fake_table.items.append(
        {"session_id": "s1", "sk": "TURN#0000", "turn_n": 0, "input_tokens": 10, "output_tokens": 5, "latency_ms": 100}
    )
    breakdown = store_dynamodb.get_token_breakdown("s1")
    assert breakdown == [
        {
            "turn_n": 0,
            "input_tokens": 10,
            "output_tokens": 5,
            "latency_ms": 100,
            "cache_read_input_tokens": 0,
            "cache_write_input_tokens": 0,
        }
    ]


def test_record_session_writes_session_turns_and_tool_calls(fake_table):
    loop_result = {
        "text": "answer",
        "trace": [{"tool": "list_services", "args": {}, "status": "ok"}],
        "turns": [{"input_tokens": 10, "output_tokens": 5, "latency_ms": 100}],
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "latency_ms": 100,
    }
    session_id = store_dynamodb.record_session("q", "us.anthropic.claude-sonnet-4-6", loop_result)
    sks = sorted(i["sk"] for i in fake_table.items if i["session_id"] == session_id)
    assert sks == ["SESSION", "TOOLCALL#0000", "TURN#0000"]


def test_record_session_without_context_blocks_key_does_not_crash(fake_table):
    loop_result = {
        "text": "answer",
        "trace": [{"tool": "list_services", "args": {}, "status": "ok"}],
        "turns": [{"input_tokens": 10, "output_tokens": 5, "latency_ms": 100}],
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "latency_ms": 100,
    }
    session_id = store_dynamodb.record_session("q", "us.anthropic.claude-sonnet-4-6", loop_result)
    assert store_dynamodb.get_context_timeline(session_id) == []


def test_context_timeline_round_trip_and_cumulative_math(fake_table):
    loop_result = {
        "text": "answer",
        "trace": [],
        "turns": [{"input_tokens": 10, "output_tokens": 5, "latency_ms": 100}],
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "latency_ms": 100,
        "context_blocks": [
            {"category": "system", "label": "System prompt", "char_count": 400, "token_estimate": 100, "turn_n": None},
            {"category": "user", "label": "User prompt", "char_count": 40, "token_estimate": 10, "turn_n": 0},
            {
                "category": "tool_result",
                "label": "Tool result: list_services",
                "char_count": 200,
                "token_estimate": 50,
                "turn_n": 0,
                "status": "error",
            },
        ],
    }
    session_id = store_dynamodb.record_session("q", "us.anthropic.claude-sonnet-4-6", loop_result)
    timeline = store_dynamodb.get_context_timeline(session_id)
    assert [b["category"] for b in timeline] == ["system", "user", "tool_result"]
    assert [b["cumulative_tokens"] for b in timeline] == [100, 110, 160]
    assert timeline[0]["turn_n"] is None
    assert timeline[2]["status"] == "error"
    assert timeline[0]["status"] is None
    assert timeline[2]["cumulative_pct"] == 0.08


def _basic_loop_result(**overrides):
    result = {
        "text": "answer",
        "trace": [{"tool": "list_services", "args": {}, "status": "ok"}],
        "turns": [{"input_tokens": 10, "output_tokens": 5, "latency_ms": 100}],
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "latency_ms": 100,
    }
    result.update(overrides)
    return result


def test_owner_none_sees_everything(fake_table):
    store_dynamodb.record_session("q1", "m", _basic_loop_result(), owner="alice-sub")
    store_dynamodb.record_session("q2", "m", _basic_loop_result(), owner="bob-sub")
    store_dynamodb.record_session("q3", "m", _basic_loop_result())
    assert len(store_dynamodb.get_recent_sessions(limit=10)) == 3


def test_owner_only_sees_own_sessions(fake_table):
    store_dynamodb.record_session("alice's q", "m", _basic_loop_result(), owner="alice-sub")
    store_dynamodb.record_session("bob's q", "m", _basic_loop_result(), owner="bob-sub")

    alice_sessions = store_dynamodb.get_recent_sessions(limit=10, owner="alice-sub")
    assert len(alice_sessions) == 1
    assert alice_sessions[0]["prompt"] == "alice's q"


def test_owner_cannot_read_another_owners_session_by_id(fake_table):
    session_id = store_dynamodb.record_session("alice's q", "m", _basic_loop_result(), owner="alice-sub")

    assert store_dynamodb.get_session_metrics(session_id, owner="bob-sub") is None
    assert store_dynamodb.get_token_breakdown(session_id, owner="bob-sub") == []
    assert store_dynamodb.get_agent_trace(session_id, owner="bob-sub") == []
    assert store_dynamodb.get_context_timeline(session_id, owner="bob-sub") == []
    assert store_dynamodb.get_tool_metrics(session_id, owner="bob-sub") == []
    assert store_dynamodb.get_cost_estimate(session_id, owner="bob-sub") is None

    assert store_dynamodb.get_session_metrics(session_id, owner="alice-sub") is not None
    assert store_dynamodb.get_session_metrics(session_id, owner=None) is not None


def test_owner_aggregate_cost_and_tool_metrics_filtered(fake_table):
    store_dynamodb.record_session("alice's q", "m", _basic_loop_result(), owner="alice-sub")
    store_dynamodb.record_session("bob's q", "m", _basic_loop_result(), owner="bob-sub")

    alice_cost = store_dynamodb.get_cost_estimate(owner="alice-sub")
    total_cost = store_dynamodb.get_cost_estimate()
    assert 0 < alice_cost < total_cost

    alice_tools = store_dynamodb.get_tool_metrics(owner="alice-sub")
    assert len(alice_tools) == 1
    all_tools = store_dynamodb.get_tool_metrics()
    assert all_tools[0]["calls"] == 2


def test_owner_defaults_to_none_backward_compatible(fake_table):
    session_id = store_dynamodb.record_session("q", "m", _basic_loop_result())
    assert store_dynamodb.get_session_metrics(session_id) is not None
    assert len(store_dynamodb.get_recent_sessions()) == 1


def test_owner_filtering_survives_scan_pagination(fake_table):
    """get_recent_sessions/get_cost_estimate/get_tool_metrics with an
    owner filter all go through _scan_all, which pages. Owner filtering
    must apply across every page, not just the first; otherwise
    alice's later-paginated sessions would silently vanish from her own
    view."""
    for i in range(5):
        store_dynamodb.record_session(f"alice's q{i}", "m", _basic_loop_result(), owner="alice-sub")
    for i in range(5):
        store_dynamodb.record_session(f"bob's q{i}", "m", _basic_loop_result(), owner="bob-sub")
    # Force every scan (get_recent_sessions, get_cost_estimate,
    # get_tool_metrics's _owned_session_ids lookup) to page one item at
    # a time. The real bug this guards against only shows up past a
    # single page.
    fake_table.page_size = 1

    alice_sessions = store_dynamodb.get_recent_sessions(limit=10, owner="alice-sub")
    assert len(alice_sessions) == 5
    assert all(s["prompt"].startswith("alice's") for s in alice_sessions)

    alice_cost = store_dynamodb.get_cost_estimate(owner="alice-sub")
    total_cost = store_dynamodb.get_cost_estimate()
    assert alice_cost * 2 == pytest.approx(total_cost)

    alice_tools = store_dynamodb.get_tool_metrics(owner="alice-sub")
    assert alice_tools[0]["calls"] == 5
    all_tools = store_dynamodb.get_tool_metrics()
    assert all_tools[0]["calls"] == 10


def test_start_or_get_session_is_idempotent(fake_table):
    """OTLP payloads can arrive out of order or get retried by the
    client: calling start_or_get_session twice for the same session_id
    must not create a second row or reset any totals already appended."""
    sid = store_dynamodb.start_or_get_session("otel-1", owner="u1", source="claude_code", model="claude-sonnet-4-5")
    store_dynamodb.append_turn(sid, {"input_tokens": 100, "output_tokens": 20, "latency_ms": 500})
    store_dynamodb.start_or_get_session("otel-1", owner="u1", source="claude_code", model="claude-sonnet-4-5")

    assert len(store_dynamodb.get_recent_sessions(owner="u1")) == 1
    metrics = store_dynamodb.get_session_metrics("otel-1", owner="u1")
    assert metrics["prompt_metrics"]["input_tokens"] == 100


def test_start_or_get_session_conditional_put_does_not_raise(fake_table):
    """DynamoDB-specific: the real conditional put races two callers
    creating the same session_id at once. DynamoDB rejects the loser's
    write with ConditionalCheckFailedException rather than letting both
    "create" it. start_or_get_session must swallow that exception
    silently (not propagate it, not duplicate the row) when the session
    already exists by the time the conditional put runs."""
    sid = store_dynamodb.start_or_get_session("otel-race", source="claude_code")
    # Simulate a second concurrent caller for the same session_id. This
    # must hit the ConditionExpression failure path in put_item and be
    # caught, not raised.
    result = store_dynamodb.start_or_get_session("otel-race", source="claude_code")
    assert result == sid
    assert len([i for i in fake_table.items if i["session_id"] == sid and i["sk"] == "SESSION"]) == 1


def test_append_turn_retries_past_index_collision(fake_table, monkeypatch):
    """Two concurrent OTLP batches for the same session can both call
    _next_index and get the same count back before either write commits
    A plain put_item would let the second silently overwrite the
    first (the DynamoDB append-race gap noted in plan.md). The
    attribute_not_exists(sk) ConditionExpression must reject the
    collision and _put_next_indexed must retry with a freshly
    recomputed index rather than losing either row."""
    sid = store_dynamodb.start_or_get_session("otel-append-race", source="claude_code")

    # Simulate a concurrent writer that already claimed index 0.
    fake_table.items.append(
        {
            "session_id": sid,
            "sk": "TURN#0000",
            "turn_n": 0,
            "input_tokens": 1,
            "output_tokens": 1,
            "latency_ms": 1,
            "cache_read_input_tokens": 0,
            "cache_write_input_tokens": 0,
        }
    )

    real_next_index = store_dynamodb._next_index
    calls = {"n": 0}

    def stale_first_call(session_id, sk_prefix):
        calls["n"] += 1
        if calls["n"] == 1:
            return 0  # stale: computed before the "concurrent" row above landed
        return real_next_index(session_id, sk_prefix)

    monkeypatch.setattr(store_dynamodb, "_next_index", stale_first_call)
    store_dynamodb.append_turn(sid, {"input_tokens": 100, "output_tokens": 20, "latency_ms": 500})

    turn_rows = [i for i in fake_table.items if i["session_id"] == sid and i["sk"].startswith("TURN#")]
    assert {r["sk"] for r in turn_rows} == {"TURN#0000", "TURN#0001"}
    original = next(r for r in turn_rows if r["sk"] == "TURN#0000")
    assert original["input_tokens"] == 1  # untouched, not overwritten by the retrying writer


def test_append_turn_accumulates_session_totals(fake_table):
    """A live Claude Code session reports turns one at a time. The
    parent session row's totals must reflect the running sum, not just
    the most recently appended turn."""
    sid = store_dynamodb.start_or_get_session("otel-2", source="claude_code", model="us.anthropic.claude-sonnet-4-6")
    store_dynamodb.append_turn(sid, {"input_tokens": 100, "output_tokens": 20, "latency_ms": 500})
    store_dynamodb.append_turn(sid, {"input_tokens": 50, "output_tokens": 10, "latency_ms": 300})

    metrics = store_dynamodb.get_session_metrics(sid)["prompt_metrics"]
    assert metrics["input_tokens"] == 150
    assert metrics["output_tokens"] == 30
    assert metrics["total_tokens"] == 180
    assert metrics["latency_ms"] == 800
    assert metrics["estimated_cost"] > 0

    breakdown = store_dynamodb.get_token_breakdown(sid)
    assert [t["turn_n"] for t in breakdown] == [0, 1]


def test_append_tool_call_increments_count(fake_table):
    sid = store_dynamodb.start_or_get_session("otel-3", source="claude_code")
    store_dynamodb.append_tool_call(sid, {"tool": "Read", "args": {"path": "x"}, "status": "success"})
    store_dynamodb.append_tool_call(sid, {"tool": "Edit", "args": {}, "status": "success"})

    assert store_dynamodb.get_session_metrics(sid)["prompt_metrics"]["tool_call_count"] == 2
    trace = store_dynamodb.get_agent_trace(sid)
    assert [t["tool"] for t in trace] == ["Read", "Edit"]


def test_append_context_block_orders_by_arrival(fake_table):
    sid = store_dynamodb.start_or_get_session("otel-4", source="claude_code")
    store_dynamodb.append_context_block(
        sid, {"category": "system", "label": "system prompt", "char_count": 400, "token_estimate": 100, "turn_n": 0}
    )
    store_dynamodb.append_context_block(
        sid, {"category": "user", "label": "user turn", "char_count": 40, "token_estimate": 10, "turn_n": 0}
    )

    timeline = store_dynamodb.get_context_timeline(sid)
    assert [b["category"] for b in timeline] == ["system", "user"]
    assert timeline[-1]["cumulative_tokens"] == 110


def test_append_context_block_round_trips_content(fake_table):
    """Same contract as store_sqlite.py's version: a block's raw text
    round-trips through storage; a block that never sets `content`
    reads back as None, not a crash or an empty string."""
    sid = store_dynamodb.start_or_get_session("otel-content", source="claude_code")
    store_dynamodb.append_context_block(
        sid,
        {
            "category": "system",
            "label": "System prompt",
            "char_count": 20,
            "token_estimate": 5,
            "turn_n": None,
            "content": "You are a helpful assistant.",
        },
    )
    store_dynamodb.append_context_block(
        sid, {"category": "user", "label": "User message", "char_count": 40, "token_estimate": 10, "turn_n": 0}
    )

    timeline = store_dynamodb.get_context_timeline(sid)
    assert timeline[0]["content"] == "You are a helpful assistant."
    assert timeline[1]["content"] is None


def test_close_session_marks_status_and_applies_final_totals(fake_table):
    """close_session's final_totals overwrite the incrementally-summed
    values with the client's own exact final report, when given."""
    sid = store_dynamodb.start_or_get_session("otel-5", source="claude_code", model="us.anthropic.claude-sonnet-4-6")
    store_dynamodb.append_turn(sid, {"input_tokens": 10, "output_tokens": 5, "latency_ms": 50})
    assert store_dynamodb.get_session_metrics(sid)["session"]["status"] == "open"

    store_dynamodb.close_session(
        sid,
        final_totals={
            "input_tokens": 999,
            "output_tokens": 111,
            "total_tokens": 1110,
            "latency_ms": 5000,
        },
    )

    metrics = store_dynamodb.get_session_metrics(sid)
    assert metrics["session"]["status"] == "closed"
    assert metrics["prompt_metrics"]["input_tokens"] == 999
    assert metrics["prompt_metrics"]["total_tokens"] == 1110


def test_close_session_without_final_totals_just_closes(fake_table):
    sid = store_dynamodb.start_or_get_session("otel-6", source="claude_code")
    store_dynamodb.append_turn(sid, {"input_tokens": 10, "output_tokens": 5, "latency_ms": 50})
    store_dynamodb.close_session(sid)

    metrics = store_dynamodb.get_session_metrics(sid)
    assert metrics["session"]["status"] == "closed"
    assert metrics["prompt_metrics"]["input_tokens"] == 10


def test_recent_sessions_carries_source_and_status(fake_table):
    """Dashboard session list needs a per-row source badge and pressure
    signal, so both bedrock_agent (legacy) and OTLP-sourced sessions must
    carry these fields the same way."""
    store_dynamodb.record_session("q", "m", _basic_loop_result())
    store_dynamodb.start_or_get_session("otel-7", source="copilot")

    recent = {s["session_id"]: s for s in store_dynamodb.get_recent_sessions()}
    sources = {s["source"] for s in recent.values()}
    assert sources == {"bedrock_agent", "copilot"}
    assert all("status" in s for s in recent.values())


def test_recent_sessions_carries_turn_count(fake_table):
    """The session-list row needs "N turns" without a per-row follow-up
    fetch, so get_recent_sessions must return the count directly (a
    bounded per-session Query, not a full-table Scan)."""
    three_turns = _basic_loop_result(
        turns=[
            {"input_tokens": 10, "output_tokens": 5, "latency_ms": 100},
            {"input_tokens": 20, "output_tokens": 5, "latency_ms": 100},
            {"input_tokens": 30, "output_tokens": 5, "latency_ms": 100},
        ]
    )
    three_turn_id = store_dynamodb.record_session("q", "m", three_turns)
    one_turn_id = store_dynamodb.record_session("q2", "m", _basic_loop_result())

    recent = {s["session_id"]: s for s in store_dynamodb.get_recent_sessions()}
    assert recent[three_turn_id]["turn_count"] == 3
    assert recent[one_turn_id]["turn_count"] == 1
