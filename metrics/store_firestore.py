"""Firestore backend for the execution recorder, an alternative to
store_dynamodb.py for deployments that prefer GCP/Firestore over AWS.
Same function signatures as store_sqlite.py/store_dynamodb.py; callers
(app.py, mcp_server/server.py) go through store.py's dispatcher and
never know which backend is active.

Schema: a top-level `sessions` collection, one document per
session_id, with `owner` and `timestamp` as top-level fields so they're
directly queryable/filterable/orderable (Firestore has no scan-then-
filter equivalent worth using when a real query is available. This is
the thing store_dynamodb.py's own docstring flags as a scan-based
tradeoff to revisit; here we design it away up front). Subcollections
`sessions/{session_id}/turns`, `.../tool_calls`, `.../context_blocks`
hold the per-turn/per-call/per-block rows, each doc ID a zero-padded
sequence number ("0000", "0001", ...) mirroring store_sqlite.py's
turn_n/seq columns. Each doc also carries that same index in a plain
`_seq` integer field, because real Firestore rejects a descending orderBy on
the document-ID/name field ("does not support descending key scans"),
which the transactional max-sequence lookup needs, so `_seq` exists
purely to give that query something descending-sortable to read.
Forward-ordered reads use `_seq` too, for consistency.

Client uses Application Default Credentials, which works automatically on
Cloud Run via its service account; for local/test use, the client
library transparently honors FIRESTORE_EMULATOR_HOST if set, no
special-casing needed here.
"""

import os
import time
import uuid

from google.api_core.exceptions import GoogleAPICallError
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from mci_common.pricing import estimate_cost
from mci_common.timeline import build_timeline
from metrics.errors import SessionOwnershipError

# Overridable so tests (and anyone running multiple logical "databases"
# against one emulator/project) can point at an isolated top-level
# collection instead of colliding on "sessions".
SESSIONS_COLLECTION = os.environ.get("METRICS_FIRESTORE_COLLECTION", "sessions")

_SEQ_WIDTH = 4  # "0000".."9999", matching store_dynamodb.py's TURN#0000-style padding

# Per-session aggregates maintained on the session doc so
# get_recent_sessions can return the session-list row's counts/sums
# without a per-session fan-out read of the turns/tool_calls
# subcollections (billed per doc on the Firestore backend). Written by
# record_session, seeded by start_or_get_session, kept current with
# firestore.Increment inside append_turn / append_tool_call's existing
# session-doc transaction. Legacy docs without them fall back (see
# get_recent_sessions).
_AGG_FIELDS = ("agg_turn_count", "agg_cache_read_tokens", "agg_fresh_input_tokens", "agg_tool_call_errors")
# No agg_tool_call_total: the existing tool_call_count field already
# tracks it, and is reused directly.


def _client():
    # A fresh Client() per call, same spirit as store_sqlite.py's
    # _connect(). Cheap (it's just a config object; the underlying gRPC
    # channel is pooled internally) and means nothing here holds a
    # module-global client that a test fixture would need to monkeypatch
    # around. Firestore's Client() picks up FIRESTORE_EMULATOR_HOST from
    # the environment on its own.
    return firestore.Client()


def _sessions(client):
    return client.collection(SESSIONS_COLLECTION)


def _visible(session_owner, caller_owner):
    """caller_owner=None means "the admin/owner token", which sees
    everything. Otherwise a caller only sees sessions it owns. Same
    contract as store_sqlite.py's version."""
    return caller_owner is None or session_owner == caller_owner


def _session_owner(client, session_id):
    doc = _sessions(client).document(session_id).get()
    return doc.get("owner") if doc.exists else None


def _seq_doc_id(n):
    return str(n).zfill(_SEQ_WIDTH)


def _next_seq_transactional(transaction, subcollection_ref):
    """Reads the current max sequence number in `subcollection_ref` and
    returns the next one to use. Called inside a @firestore.transactional
    function so the read-then-decide-the-next-id step and the following
    write are serialized against concurrent appends to the same
    subcollection. This is Firestore's native replacement for
    store_dynamodb.py's manual attribute_not_exists(sk)-plus-retry-loop
    pattern: the transaction itself aborts and retries on conflict, so
    there's no hand-rolled retry cap to maintain here.

    Orders by an explicit `_seq` integer field, not FieldPath.document_id()
    Real Firestore (unlike some emulator versions) rejects a descending
    orderBy on the document-ID/name field ("Firestore does not support
    descending key scans"), so every sequenced doc carries its own
    zero-padded ID *and* this plain integer field purely so this query has
    something descending-sortable to read."""
    docs = list(transaction.get(subcollection_ref.order_by("_seq", direction=firestore.Query.DESCENDING).limit(1)))
    return docs[0].get("_seq") + 1 if docs else 0


def record_session(prompt, model_id, loop_result, owner=None):
    """loop_result is runtime.run_agent_loop()'s return dict. owner is
    the Google account `sub` that recorded this session, or None for
    the server owner's own; see _visible(). This stays the one-shot
    atomic(-ish) path (source='bedrock_agent', status 'closed'
    immediately). OTLP ingestion uses start_or_get_session + append_*
    below instead, since that data arrives incrementally. Uses a batch
    write: not a full cross-document transaction, but all writes commit
    or none do, which is the atomicity property record_session actually
    needs (nothing reads this session mid-write)."""
    client = _client()
    session_id = str(uuid.uuid4())
    cost = estimate_cost(model_id, loop_result["input_tokens"], loop_result["output_tokens"])
    ts = time.time()

    batch = client.batch()
    session_ref = _sessions(client).document(session_id)
    batch.set(
        session_ref,
        {
            "session_id": session_id,
            "prompt": prompt,
            "model": model_id,
            "input_tokens": loop_result["input_tokens"],
            "output_tokens": loop_result["output_tokens"],
            "total_tokens": loop_result["total_tokens"],
            "latency_ms": loop_result["latency_ms"],
            "tool_call_count": len(loop_result["trace"]),
            "estimated_cost": cost,
            "timestamp": ts,
            "owner": owner,
            "source": "bedrock_agent",
            "status": "closed",
            # Denormalised session-list aggregates (see _AGG_FIELDS).
            "agg_turn_count": len(loop_result["turns"]),
            "agg_cache_read_tokens": sum(t.get("cache_read_input_tokens", 0) for t in loop_result["turns"]),
            "agg_fresh_input_tokens": sum(t["input_tokens"] for t in loop_result["turns"]),
            "agg_tool_call_errors": sum(1 for c in loop_result["trace"] if c.get("status") == "error"),
        },
    )
    for i, turn in enumerate(loop_result["turns"]):
        batch.set(
            session_ref.collection("turns").document(_seq_doc_id(i)),
            {
                "_seq": i,
                "turn_n": i,
                "input_tokens": turn["input_tokens"],
                "output_tokens": turn["output_tokens"],
                "latency_ms": turn["latency_ms"],
                "cache_read_input_tokens": turn.get("cache_read_input_tokens", 0),
                "cache_write_input_tokens": turn.get("cache_write_input_tokens", 0),
            },
        )
    for i, call in enumerate(loop_result["trace"]):
        batch.set(
            session_ref.collection("tool_calls").document(_seq_doc_id(i)),
            {
                "_seq": i,
                "tool_name": call["tool"],
                "args": call["args"],
                "status": call["status"],
                "latency_ms": call.get("latency_ms", 0),
                "timestamp": call.get("timestamp", ts),
            },
        )
    for i, block in enumerate(loop_result.get("context_blocks", [])):
        batch.set(
            session_ref.collection("context_blocks").document(_seq_doc_id(i)),
            {
                "_seq": i,
                "category": block["category"],
                "label": block["label"],
                "char_count": block["char_count"],
                "token_estimate": block["token_estimate"],
                "turn_n": block.get("turn_n"),
                "status": block.get("status"),
                "content": block.get("content"),
            },
        )
    batch.commit()
    return session_id


def get_session_metrics(session_id, owner=None):
    """Session metadata split from per-prompt processing metrics, same
    shape contract as store_sqlite.py's version (see that docstring,
    including why a wrong-owner lookup returns None instead of raising:
    it must read identically to "session doesn't exist" so this can't
    be used to probe which session_ids exist)."""
    client = _client()
    doc = _sessions(client).document(session_id).get()
    if not doc.exists:
        return None
    data = doc.to_dict()
    if not _visible(data.get("owner"), owner):
        return None
    return {
        "session": {
            "session_id": data["session_id"],
            "model": data["model"],
            "timestamp": data["timestamp"],
            "source": data.get("source") or "bedrock_agent",
            "status": data.get("status") or "closed",
        },
        "prompt_metrics": {
            "prompt": data.get("prompt"),
            "input_tokens": data["input_tokens"],
            "output_tokens": data["output_tokens"],
            "total_tokens": data["total_tokens"],
            "latency_ms": data["latency_ms"],
            "tool_call_count": data["tool_call_count"],
            "estimated_cost": data["estimated_cost"],
        },
    }


def get_token_breakdown(session_id, owner=None):
    client = _client()
    if not _visible(_session_owner(client, session_id), owner):
        return []
    docs = _sessions(client).document(session_id).collection("turns").order_by("_seq").stream()
    result = []
    for d in docs:
        data = d.to_dict()
        result.append(
            {
                "turn_n": data["turn_n"],
                "input_tokens": data["input_tokens"],
                "output_tokens": data["output_tokens"],
                "latency_ms": data["latency_ms"],
                "cache_read_input_tokens": data.get("cache_read_input_tokens", 0),
                "cache_write_input_tokens": data.get("cache_write_input_tokens", 0),
            }
        )
    return result


def _aggregate_tool_counts(tool_call_docs):
    counts = {}
    for d in tool_call_docs:
        data = d.to_dict()
        key = (data["tool_name"], data["status"])
        counts[key] = counts.get(key, 0) + 1
    return [{"tool_name": t, "status": s, "calls": c} for (t, s), c in counts.items()]


def get_tool_metrics(session_id=None, owner=None):
    """Three paths, same as store_sqlite.py/store_dynamodb.py: a single
    session's tool_calls subcollection; every session owned by `owner`;
    or every session, admin-wide. Firestore has no server-side JOIN, so
    the owner-filtered and global paths read each matching session's
    tool_calls subcollection individually and aggregate in Python. An
    N-subcollection-read fan-out, acceptable at personal-project scale
    (same tradeoff store_dynamodb.py's own comments call out for its
    scan-based aggregates)."""
    client = _client()
    if session_id:
        if not _visible(_session_owner(client, session_id), owner):
            return []
        docs = _sessions(client).document(session_id).collection("tool_calls").stream()
        return _aggregate_tool_counts(docs)

    if owner is not None:
        session_docs = _sessions(client).where(filter=FieldFilter("owner", "==", owner)).stream()
    else:
        session_docs = _sessions(client).stream()

    counts = {}
    for sdoc in session_docs:
        for d in sdoc.reference.collection("tool_calls").stream():
            data = d.to_dict()
            key = (data["tool_name"], data["status"])
            counts[key] = counts.get(key, 0) + 1
    return [{"tool_name": t, "status": s, "calls": c} for (t, s), c in counts.items()]


def get_agent_trace(session_id, owner=None):
    """args is stored as a dict directly in Firestore (nested objects
    are native), so unlike store_sqlite.py's json.loads round trip,
    nothing needs deserializing here."""
    client = _client()
    if not _visible(_session_owner(client, session_id), owner):
        return []
    docs = _sessions(client).document(session_id).collection("tool_calls").order_by("_seq").stream()
    result = []
    for d in docs:
        data = d.to_dict()
        result.append(
            {
                "tool": data["tool_name"],
                "args": data.get("args") or {},
                "status": data["status"],
                "latency_ms": data.get("latency_ms") or 0,
                "timestamp": data.get("timestamp") or 0,
            }
        )
    return result


def get_cost_estimate(session_id=None, period_seconds=None, owner=None):
    client = _client()
    if session_id:
        metrics = get_session_metrics(session_id, owner=owner)
        # 0.0, not None: the tool contract is `-> float`. An unknown or
        # non-owned session has no cost to report, same "no data" answer
        # the list tools give with [].
        return metrics["prompt_metrics"]["estimated_cost"] if metrics else 0.0

    since = time.time() - period_seconds if period_seconds else 0
    query = _sessions(client).where(filter=FieldFilter("timestamp", ">=", since))
    if owner is not None:
        query = query.where(filter=FieldFilter("owner", "==", owner))

    # Prefer Firestore's native sum() aggregation over pulling every
    # matching document just to add one field client-side. Fall back to a
    # Python sum if the installed client lacks aggregation support
    # (AttributeError) OR the backend rejects the aggregation for want of
    # a composite index (GoogleAPICallError / FailedPrecondition) -- the
    # stream() path needs no composite index, so it always works.
    try:
        agg = query.sum("estimated_cost").get()
        total = agg[0][0].value
        return total or 0.0
    except (AttributeError, GoogleAPICallError):
        return sum(doc.get("estimated_cost") or 0.0 for doc in query.stream()) or 0.0


def get_context_timeline(session_id, owner=None):
    """Ordered, categorized breakdown of everything that entered this
    session's context window, same contract as store_sqlite.py's
    version (see that docstring). build_timeline is backend-agnostic;
    reused as-is."""
    client = _client()
    if not _visible(_session_owner(client, session_id), owner):
        return []
    docs = _sessions(client).document(session_id).collection("context_blocks").order_by("_seq").stream()
    rows = (
        {
            "category": data["category"],
            "label": data["label"],
            "char_count": data["char_count"],
            "token_estimate": data["token_estimate"],
            "turn_n": data.get("turn_n"),
            "status": data.get("status"),
            "content": data.get("content"),
        }
        for data in (d.to_dict() for d in docs)
    )
    return build_timeline(rows)


def get_recent_sessions(limit=10, owner=None, include_test_sessions=False):
    """Requires a composite index on (owner ASC, timestamp DESC) in
    production for the owner-filtered query below to run without
    Firestore rejecting it. Deploying that index is a separate task,
    not handled here.

    Per-session aggregates (turn_count, cache_read_tokens,
    fresh_input_tokens, tool_call_total, tool_call_errors) are kept
    denormalised on the session doc (see _AGG_FIELDS), so the common path
    is a single `limit` query with no subcollection reads. A doc written
    before those fields existed (any _AGG_FIELDS key missing) falls back
    to the per-session turns/tool_calls fan-out for that row only.

    include_test_sessions=False (the default) drops rows whose
    session_id starts with "api-tests-"; see store_sqlite.py's
    get_recent_sessions docstring. Filtered client-side after `limit` is
    already applied server-side, so a page that's entirely test data can
    come back empty rather than backfilled. Acceptable at this scale,
    same as the rest of this function's tradeoffs."""
    client = _client()
    query = _sessions(client)
    if owner is not None:
        query = query.where(filter=FieldFilter("owner", "==", owner))
    query = query.order_by("timestamp", direction=firestore.Query.DESCENDING).limit(limit)

    result = []
    for doc in query.stream():
        data = doc.to_dict()
        if not include_test_sessions and data["session_id"].startswith("api-tests-"):
            continue

        if all(k in data for k in _AGG_FIELDS):
            # Fast path: counts/sums are denormalised on the session doc.
            aggregates = {
                "turn_count": data["agg_turn_count"],
                "cache_read_tokens": data["agg_cache_read_tokens"],
                "fresh_input_tokens": data["agg_fresh_input_tokens"],
                "tool_call_total": data.get("tool_call_count", 0),
                "tool_call_errors": data["agg_tool_call_errors"],
            }
        else:
            # Legacy doc (predates the denormalised fields): fall back to
            # the per-session subcollection fan-out for this row only.
            turns = [t.to_dict() for t in doc.reference.collection("turns").stream()]
            tool_calls = [t.to_dict() for t in doc.reference.collection("tool_calls").stream()]
            aggregates = {
                "turn_count": len(turns),
                "cache_read_tokens": sum(t.get("cache_read_input_tokens", 0) for t in turns),
                "fresh_input_tokens": sum(t.get("input_tokens", 0) for t in turns),
                "tool_call_total": len(tool_calls),
                "tool_call_errors": sum(1 for c in tool_calls if c.get("status") == "error"),
            }

        result.append(
            {
                "session_id": data["session_id"],
                "prompt": data.get("prompt"),
                "model": data["model"],
                "total_tokens": data["total_tokens"],
                "estimated_cost": data["estimated_cost"],
                "timestamp": data["timestamp"],
                "source": data.get("source") or "bedrock_agent",
                "status": data.get("status") or "closed",
                **aggregates,
            }
        )
    return result


def start_or_get_session(session_id, owner=None, source="claude_code", model=None):
    """Entry point for OTLP ingestion; see store_sqlite.py's docstring
    for the full reasoning; same contract here. Runs as a Firestore
    transaction (read the doc, then create-if-missing or check
    ownership) so two concurrent callers racing to create the same
    brand-new session_id can't both "win". Firestore's transaction
    serialization aborts and retries the loser rather than the manual
    conditional-put-plus-catch pattern store_dynamodb.py needs."""
    client = _client()
    session_ref = _sessions(client).document(session_id)

    @firestore.transactional
    def _txn(transaction):
        snapshot = session_ref.get(transaction=transaction)
        if snapshot.exists:
            existing_owner = snapshot.get("owner")
            if not _visible(existing_owner, owner):
                raise SessionOwnershipError(f"session_id {session_id!r} belongs to a different owner")
            return
        transaction.set(
            session_ref,
            {
                "session_id": session_id,
                "prompt": None,
                "model": model,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "latency_ms": 0,
                "tool_call_count": 0,
                "estimated_cost": 0.0,
                "timestamp": time.time(),
                "owner": owner,
                "source": source,
                "status": "open",
                # Denormalised session-list aggregates (see _AGG_FIELDS);
                # kept current by append_turn / append_tool_call.
                "agg_turn_count": 0,
                "agg_cache_read_tokens": 0,
                "agg_fresh_input_tokens": 0,
                "agg_tool_call_errors": 0,
            },
        )

    _txn(client.transaction())
    return session_id


def _check_ownership_or_raise(client, session_id, owner):
    """Same reasoning as store_sqlite.py's version: every append_*/
    close_session call must not be able to write into a session_id
    owned by someone else. A missing session (append called before
    start_or_get_session) is treated as belonging to no one, i.e.
    deny-by-default for any non-admin caller.

    Used by append_context_block and close_session. append_turn /
    append_tool_call enforce ownership in-transaction instead (see
    _raise_if_not_visible)."""
    if not _visible(_session_owner(client, session_id), owner):
        raise SessionOwnershipError(f"session_id {session_id!r} belongs to a different owner")


def _raise_if_not_visible(snapshot, session_id, owner):
    """Ownership enforcement from an already-read session snapshot, for
    the transactional append paths. A non-existent session (snapshot
    missing) is owned by no one -> deny for any non-admin caller, exactly
    as _check_ownership_or_raise's _session_owner(None) case does."""
    session_owner = snapshot.get("owner") if snapshot.exists else None
    if not _visible(session_owner, owner):
        raise SessionOwnershipError(f"session_id {session_id!r} belongs to a different owner")


def _recost_session(client, session_id):
    """Re-derive estimated_cost from the session's own model + running
    token totals, same reasoning as store_sqlite.py's version. Used by
    close_session's final_totals override path (totals set outside a
    transaction, so a follow-up read is the only way to recost).
    append_turn recosts inside its own transaction instead."""
    doc = _sessions(client).document(session_id).get()
    if not doc.exists:
        return
    data = doc.to_dict()
    cost = estimate_cost(data["model"], data["input_tokens"], data["output_tokens"])
    _sessions(client).document(session_id).update({"estimated_cost": cost})


def _agg_increment_or_backfill(transaction, session_ref, prev, *, turn_delta, cache_read_delta, fresh_input_delta):
    """Return the session-doc field updates that keep the denormalised
    aggregates current for one append_turn.

    Normal case: the doc already has the agg fields -> firestore.Increment
    deltas. Legacy case: a pre-denormalisation doc lacks them, so a plain
    Increment would count only from now on; detect that (agg_turn_count
    absent) and backfill true totals by scanning the subcollections once.
    All reads here happen before the caller's transaction writes.
    """
    if "agg_turn_count" in prev:
        return {
            "agg_turn_count": firestore.Increment(turn_delta),
            "agg_cache_read_tokens": firestore.Increment(cache_read_delta),
            "agg_fresh_input_tokens": firestore.Increment(fresh_input_delta),
        }
    # order_by("_seq") only because transaction.get needs a Query, not a
    # bare CollectionReference; order is irrelevant to the sums below.
    turns = [t.to_dict() for t in transaction.get(session_ref.collection("turns").order_by("_seq"))]
    tool_calls = [t.to_dict() for t in transaction.get(session_ref.collection("tool_calls").order_by("_seq"))]
    return {
        "agg_turn_count": len(turns) + turn_delta,
        "agg_cache_read_tokens": sum(t.get("cache_read_input_tokens", 0) for t in turns) + cache_read_delta,
        "agg_fresh_input_tokens": sum(t.get("input_tokens", 0) for t in turns) + fresh_input_delta,
        "agg_tool_call_errors": sum(1 for c in tool_calls if c.get("status") == "error"),
    }


def append_turn(session_id, turn_data, owner=None):
    """turn_data: {input_tokens, output_tokens, latency_ms,
    cache_read_input_tokens=0, cache_write_input_tokens=0}. Adds a turns
    subcollection doc and folds its totals into the parent session doc
    so get_session_metrics/get_recent_sessions stay accurate without a
    separate aggregation pass. owner must match the session's own owner
    (enforced in-transaction from the session snapshot; see
    _raise_if_not_visible)."""
    client = _client()
    session_ref = _sessions(client).document(session_id)
    turns_ref = session_ref.collection("turns")
    delta_in = turn_data["input_tokens"]
    delta_out = turn_data["output_tokens"]
    delta_cache_read = turn_data.get("cache_read_input_tokens", 0)

    @firestore.transactional
    def _txn(transaction):
        # All reads before any writes (Firestore requirement): seq
        # number, then the session doc -- reused for the ownership check
        # and the in-transaction recost.
        turn_n = _next_seq_transactional(transaction, turns_ref)
        snapshot = session_ref.get(transaction=transaction)
        _raise_if_not_visible(snapshot, session_id, owner)
        prev = snapshot.to_dict() if snapshot.exists else {}
        new_cost = estimate_cost(
            prev.get("model"),
            (prev.get("input_tokens") or 0) + delta_in,
            (prev.get("output_tokens") or 0) + delta_out,
        )
        agg_update = _agg_increment_or_backfill(
            transaction, session_ref, prev, turn_delta=1, cache_read_delta=delta_cache_read, fresh_input_delta=delta_in
        )

        transaction.set(
            turns_ref.document(_seq_doc_id(turn_n)),
            {
                "_seq": turn_n,
                "turn_n": turn_n,
                "input_tokens": delta_in,
                "output_tokens": delta_out,
                "latency_ms": turn_data["latency_ms"],
                "cache_read_input_tokens": delta_cache_read,
                "cache_write_input_tokens": turn_data.get("cache_write_input_tokens", 0),
            },
        )
        transaction.update(
            session_ref,
            {
                "input_tokens": firestore.Increment(delta_in),
                "output_tokens": firestore.Increment(delta_out),
                "total_tokens": firestore.Increment(delta_in + delta_out),
                "latency_ms": firestore.Increment(turn_data["latency_ms"]),
                "estimated_cost": new_cost,
                **agg_update,
            },
        )

    _txn(client.transaction())


def append_tool_call(session_id, tool_call, owner=None):
    """tool_call: {tool, args, status, latency_ms=0, timestamp=None
    (defaults to now)}. owner must match the session's own owner
    (enforced in-transaction from the session snapshot; see
    _raise_if_not_visible)."""
    client = _client()
    session_ref = _sessions(client).document(session_id)
    tool_calls_ref = session_ref.collection("tool_calls")
    is_error = tool_call["status"] == "error"

    @firestore.transactional
    def _txn(transaction):
        seq = _next_seq_transactional(transaction, tool_calls_ref)
        # One session-doc read, reused for the ownership check and the
        # error-count bookkeeping. A legacy doc missing
        # agg_tool_call_errors gets it backfilled once, as in append_turn.
        snapshot = session_ref.get(transaction=transaction)
        _raise_if_not_visible(snapshot, session_id, owner)
        prev = snapshot.to_dict() if snapshot.exists else {}
        if "agg_tool_call_errors" in prev:
            error_update = {"agg_tool_call_errors": firestore.Increment(1)} if is_error else {}
        else:
            existing = [t.to_dict() for t in transaction.get(tool_calls_ref.order_by("_seq"))]
            prior_errors = sum(1 for c in existing if c.get("status") == "error")
            error_update = {"agg_tool_call_errors": prior_errors + (1 if is_error else 0)}

        transaction.set(
            tool_calls_ref.document(_seq_doc_id(seq)),
            {
                "_seq": seq,
                "tool_name": tool_call["tool"],
                "args": tool_call["args"],
                "status": tool_call["status"],
                "latency_ms": tool_call.get("latency_ms", 0),
                "timestamp": tool_call.get("timestamp") or time.time(),
            },
        )
        transaction.update(session_ref, {"tool_call_count": firestore.Increment(1), **error_update})

    _txn(client.transaction())


def append_context_block(session_id, block, owner=None):
    """block: {category, label, char_count, token_estimate, turn_n,
    status=None, content=None}, the same shape record_session's
    context_blocks docs use. owner must match the session's own owner
    (see _check_ownership_or_raise).

    Also backfills the session's `prompt` field from this block's text
    the first time a category="user" block arrives on a session that
    was created via start_or_get_session (prompt=None there, since OTLP
    ingestion has no upfront prompt the way record_session does). This
    is what makes the dashboard show the actual first user message
    instead of "(no prompt)" for OTLP-sourced sessions. Truncated to
    ~200 chars (a label, not a full transcript; the block's own content
    field already holds the full text, no need to store it twice).
    Guarded on prompt still being empty so a later turn's user message
    can't clobber turn 0's."""
    client = _client()
    _check_ownership_or_raise(client, session_id, owner)
    session_ref = _sessions(client).document(session_id)
    blocks_ref = session_ref.collection("context_blocks")

    @firestore.transactional
    def _txn(transaction):
        # All transaction reads must happen before any writes (Firestore
        # requirement), so the session doc is read up front alongside
        # the sequence-number read, before either write below.
        seq = _next_seq_transactional(transaction, blocks_ref)
        needs_prompt_backfill = False
        if block.get("category") == "user":
            snapshot = session_ref.get(transaction=transaction)
            current_prompt = snapshot.get("prompt") if snapshot.exists else None
            needs_prompt_backfill = not current_prompt

        transaction.set(
            blocks_ref.document(_seq_doc_id(seq)),
            {
                "_seq": seq,
                "category": block["category"],
                "label": block["label"],
                "char_count": block["char_count"],
                "token_estimate": block["token_estimate"],
                "turn_n": block.get("turn_n"),
                "status": block.get("status"),
                "content": block.get("content"),
            },
        )
        if needs_prompt_backfill:
            text = block.get("content") or block.get("label") or ""
            transaction.update(session_ref, {"prompt": text[:200]})

    _txn(client.transaction())


def close_session(session_id, final_totals=None, owner=None):
    """Marks a session complete once the client process exits or goes
    idle past some timeout. Same contract as store_sqlite.py's version;
    see that docstring, including the owner check and final_totals
    override semantics."""
    client = _client()
    _check_ownership_or_raise(client, session_id, owner)
    session_ref = _sessions(client).document(session_id)
    if final_totals:
        session_ref.update(
            {
                "input_tokens": final_totals["input_tokens"],
                "output_tokens": final_totals["output_tokens"],
                "total_tokens": final_totals["total_tokens"],
                "latency_ms": final_totals["latency_ms"],
            }
        )
        _recost_session(client, session_id)
    session_ref.update({"status": "closed"})
