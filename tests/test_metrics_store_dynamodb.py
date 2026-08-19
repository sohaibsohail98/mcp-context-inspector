"""Regression tests for metrics/store_dynamodb.py, using a minimal
in-memory fake table — not moto, not real AWS. The fake only implements
the exact scan/query/get_item/batch_writer calls store_dynamodb.py
actually issues; it is not a general DynamoDB emulator.

Covers two real bugs a fresh-eyes review found before this was deployed:
1. get_session_metrics leaked the internal "sk" partition-key field,
   giving a different shape than store_sqlite.py's version of the same
   function (the two are meant to be interchangeable behind
   metrics/store.py's dispatcher).
2. Aggregate reads (get_recent_sessions, get_tool_metrics, get_cost_estimate)
   used a single unpaginated Scan call, which DynamoDB truncates once it
   hits its per-call scan limit — silently undercounting/omitting recent
   items as the table grows, with no error raised.
"""

from decimal import Decimal

import pytest

from metrics import store_dynamodb


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

    def _apply_filter(self, expr, values, names=None):
        if expr is None:
            return list(self.items)
        if expr.strip() == "sk = :sk AND #o = :owner":
            owner_attr = (names or {})["#o"]
            return [
                i for i in self.items
                if i["sk"] == values[":sk"] and i.get(owner_attr) == values[":owner"]
            ]
        if expr.startswith("begins_with"):
            prefix = values[":prefix"]
            return [i for i in self.items if i["sk"].startswith(prefix)]
        if expr.strip().startswith("sk ="):
            val = values[":sk"]
            return [i for i in self.items if i["sk"] == val]
        raise NotImplementedError(expr)

    def scan(self, FilterExpression=None, ExpressionAttributeValues=None, ExpressionAttributeNames=None, ExclusiveStartKey=None, **_):
        filtered = self._apply_filter(FilterExpression, ExpressionAttributeValues, ExpressionAttributeNames)
        start = ExclusiveStartKey or 0
        page = filtered[start : start + self.page_size]
        resp = {"Items": page}
        next_start = start + self.page_size
        if next_start < len(filtered):
            resp["LastEvaluatedKey"] = next_start
        return resp

    def query(self, KeyConditionExpression=None, ExpressionAttributeValues=None, **_):
        sid = ExpressionAttributeValues[":sid"]
        prefix = ExpressionAttributeValues.get(":prefix")
        items = [
            i for i in self.items
            if i["session_id"] == sid and (prefix is None or i["sk"].startswith(prefix))
        ]
        return {"Items": items}

    def get_item(self, Key):
        for i in self.items:
            if i["session_id"] == Key["session_id"] and i["sk"] == Key["sk"]:
                return {"Item": i}
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
    # Same session/prompt_metrics split the SQLite backend returns — this
    # is the interchangeability contract.
    assert set(metrics.keys()) == {"session", "prompt_metrics"}
    assert set(metrics["session"].keys()) == {"session_id", "model", "timestamp"}
    assert set(metrics["prompt_metrics"].keys()) == {
        "prompt", "input_tokens", "output_tokens",
        "total_tokens", "latency_ms", "tool_call_count", "estimated_cost",
    }


def test_get_session_metrics_missing_returns_none(fake_table):
    assert store_dynamodb.get_session_metrics("nope") is None


def test_scan_all_paginates_past_a_single_page(fake_table):
    """The actual bug: force a table where results span 3 pages and
    confirm every item still comes back, not just the first page."""
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
    # Newest (highest timestamp) first — would silently drop s2 if
    # pagination were broken, since it's on the last page.
    assert recent[0]["session_id"] == "s2"


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
            {"category": "tool_result", "label": "Tool result: list_services", "char_count": 200, "token_estimate": 50, "turn_n": 0, "status": "error"},
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


# --- Per-owner data isolation -----------------------------------------


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
    """The actual risk: get_recent_sessions/get_cost_estimate/get_tool_metrics
    with an owner filter all go through _scan_all, which pages. If owner
    filtering were applied only to the first page (e.g. a bug that
    filtered client-side after truncating), alice's later-paginated
    sessions would silently vanish from her own view."""
    for i in range(5):
        store_dynamodb.record_session(f"alice's q{i}", "m", _basic_loop_result(), owner="alice-sub")
    for i in range(5):
        store_dynamodb.record_session(f"bob's q{i}", "m", _basic_loop_result(), owner="bob-sub")
    # Force every scan (get_recent_sessions, get_cost_estimate,
    # get_tool_metrics's _owned_session_ids lookup) to page one item at
    # a time — the real bug this guards against only shows up past a
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
