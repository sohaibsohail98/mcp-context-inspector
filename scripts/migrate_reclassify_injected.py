"""One-off data migration: reclassify harness-injected context blocks.

BACKGROUND
----------
The Claude Code OTLP mapper (``mcp_server/otlp/claude_code.py``) used to
label the whole text of a role=user message ``category="user"`` and the
whole text of a role=assistant message ``category="answer"``. That is
wrong when the text is machine-generated context Claude Code wraps
around the real message before the model ever sees it: nine wrapper
families in two classes (see ``mcp_server.otlp.common``'s
``split_injected_context`` docstring) --

  injected: ``<system-reminder>`` (user AND assistant), ``<session>``,
    ``<ide_opened_file>``, ``<fork-boilerplate>``,
    ``<task-notification>``, ``<user-prompt-submit-hook>``
  command:  the ``<command-name>/<command-message>/<command-args>/
    <command-contents>`` group, ``<local-command-caveat>``,
    ``<local-command-stdout>/<local-command-stderr>``,
    ``<bash-input>/<bash-stdout>/<bash-stderr>``

Two new categories now exist: ``injected`` (label "Injected context")
and ``command`` (label "Slash command"). Going forward the mapper peels
each message string via ``split_injected_context``. THIS SCRIPT applies
the same peel to rows that were already stored under the old blanket
rule, using that SAME shared function so the migration and the future
mapper can never drift apart.

It also repairs ``sessions.prompt``. ``append_context_block`` backfills
``sessions.prompt`` from the first ``category == "user"`` block's
content; a historical session whose first "user" block was really a
``<system-reminder>`` (or a ``<command-*>`` group) therefore has a
corrupted prompt. After reclassification this script recomputes
``sessions.prompt`` from the first genuinely-user block.

WHY THE MIGRATION, NOT A MAPPER-SIDE COMPAT SHIM
-----------------------------------------------
``_handle_request_body`` diffs a freshly-walked block list against the
stored timeline with an occurrence-indexed multiset compare. Once the
mapper starts peeling, a freshly-walked block list for an
already-stored (un-peeled) session would no longer match, and the first
post-deploy ingest would double-append the whole tail. Rewriting every
stored session here so the shapes already match is the clean fix; the
mapper then needs no historical-compat branch. Run this migration
BEFORE deploying the mapper change.

SEQ STRATEGY
------------
``context_blocks`` rows are ordered purely by an integer ``seq`` /
``_seq`` / ``CTXBLOCK#NNNN`` sort key, and the stores allocate the next
one with ``MAX(seq) + 1`` (``store_sqlite._next_seq`` and the DynamoDB /
Firestore equivalents). Splitting one row into two needs one new
ordering slot between it and the next row. A fractional-seq scheme
(1.0, 1.5, 2.0) would break that ``+ 1`` integer assumption for every
future append. The least invasive approach that keeps ``_next_seq``
correct is a DENSE RENUMBER: rebuild the session's full ordered block
list (each split row expanded in place into its two fragments) and
rewrite every row with contiguous seqs ``0..M-1``. Order is preserved
exactly; only the integer labels change, and a subsequent live append
still lands at ``MAX(seq) + 1 == M``.

TOKEN INVARIANT
---------------
For every original row that is split into fragments, the fragment rows
satisfy, EXACTLY:

    sum(fragment.char_count)     == original.char_count
    sum(fragment.token_estimate) == original.token_estimate

The fragments are NEVER independently re-estimated (that drifts by a
token or two per cut, since ``estimate_tokens`` is ``max(1, len // 4)``
integer floor division). Instead the original row's stored
``token_estimate`` -- computed once at ingest from the full untruncated
text -- is re-divided across the fragments in proportion to their
``char_count`` via ``common.distribute_token_estimate``, with the
rounding remainder dumped on the LAST fragment. ``char_count`` is
reconciled the same way against the original row's stored value (which
can exceed ``len(content)`` when the stored content was
redacted/truncated).

Net effect: a session's total ``token_estimate`` across all
context_blocks is unchanged, so ``mci_common/timeline.py``'s
``cumulative_pct`` and the dashboard's per-category ``%`` totals are
unchanged -- the same tokens are just spread across more rows. The
script asserts a per-session total-token delta of 0 and reports it.

USAGE
-----
    uv run python -m scripts.migrate_reclassify_injected              # dry run (default)
    uv run python -m scripts.migrate_reclassify_injected --apply
    uv run python -m scripts.migrate_reclassify_injected --session <id>
    uv run python -m scripts.migrate_reclassify_injected --apply --session <id>

Honours ``STORAGE_BACKEND`` (``sqlite`` default, ``dynamodb``,
``firestore``) exactly like ``metrics/store.py``. Reuses each backend
module's own connection/query helpers; it does not open its own DB
clients.

IDEMPOTENCE
-----------
A fragment row's category is already ``injected`` / ``command`` (skipped
by category), or a prose fragment has no boundary-anchored wrapper left
(``contains_injected_wrappers`` is False -> skipped). Running ``--apply``
a second time touches nothing.
"""

import argparse
import os
import sys

from mcp_server.otlp.common import (
    CATEGORY_ANSWER,
    CATEGORY_COMMAND,
    CATEGORY_INJECTED,
    CATEGORY_LABELS,
    CATEGORY_USER,
    contains_injected_wrappers,
    distribute_int,
    distribute_token_estimate,
    split_injected_context,
)

_SPLITTABLE_CATEGORIES = {CATEGORY_USER, CATEGORY_ANSWER}

# Label the split mapper will emit for a base_category fragment, matching
# claude_code._mk_text_block.
_BASE_LABELS = {
    CATEGORY_USER: "User message",
    CATEGORY_ANSWER: "Assistant response",
}


# ---------------------------------------------------------------------------
# Fragment planning (backend-independent)
# ---------------------------------------------------------------------------


def _plan_fragments(row):
    """row: a dict with at least category, label, char_count,
    token_estimate, turn_n, status, content (the shape every backend's
    context_blocks storage uses).

    Returns None if the row should not be split (wrong category, or no
    boundary-anchored wrapper in its content). Otherwise returns a list
    of new row dicts (same shape, no seq -- the caller renumbers) that
    replace it, with the char_count / token_estimate invariant already
    reconciled to the original row's stored totals EXACTLY.
    """
    category = row["category"]
    if category not in _SPLITTABLE_CATEGORIES:
        return None
    content = row.get("content")
    if not content or not contains_injected_wrappers(content, category):
        return None

    frags = split_injected_context(content, category)
    if len(frags) <= 1:
        return None  # nothing peelable at a clean boundary

    frag_chars = [len(f) for f, _ in frags]
    # Re-divide the ORIGINAL row's stored totals across the fragments by
    # char proportion; remainder on the last fragment. Never re-estimate.
    tok_split = distribute_token_estimate(frag_chars, row["token_estimate"])
    char_split = distribute_int(frag_chars, row["char_count"])

    new_rows = []
    for (frag_text, frag_category), tok, cc in zip(frags, tok_split, char_split, strict=True):
        if frag_category in (CATEGORY_INJECTED, CATEGORY_COMMAND):
            label = CATEGORY_LABELS[frag_category]
            status = None
        else:
            label = _BASE_LABELS.get(frag_category, row["label"])
            status = row.get("status")
        new_rows.append(
            {
                "category": frag_category,
                "label": label,
                "char_count": cc,
                "token_estimate": tok,
                "turn_n": row["turn_n"],
                "status": status,
                "content": frag_text,
            }
        )

    # Post-conditions the migration guarantees, row by row.
    assert sum(r["char_count"] for r in new_rows) == row["char_count"]
    assert sum(r["token_estimate"] for r in new_rows) == row["token_estimate"]
    assert "".join(r["content"] for r in new_rows) == content
    return new_rows


def _first_genuine_user_text(rows):
    """rows: the session's context_blocks in final (post-split) seq
    order. Returns the ~200-char prompt preview from the first
    category=="user" block's content, mirroring append_context_block's
    backfill, or None if there is no user block."""
    for r in rows:
        if r["category"] == CATEGORY_USER:
            return (r.get("content") or r.get("label") or "")[:200]
    return None


def _plan_session(rows):
    """rows: the session's existing context_blocks, in seq order, each a
    dict (category, label, char_count, token_estimate, turn_n, status,
    content).

    Returns (changed: bool, new_rows: list, report: dict). new_rows is
    the full renumbered replacement list (seqs assigned by the adapter).
    """
    new_rows = []
    split_count = 0
    for row in rows:
        planned = _plan_fragments(row)
        if planned is None:
            new_rows.append(dict(row))
        else:
            split_count += 1
            new_rows.extend(planned)

    changed = split_count > 0
    old_total = sum(r["token_estimate"] for r in rows)
    new_total = sum(r["token_estimate"] for r in new_rows)
    old_prompt = _first_genuine_user_text(rows)
    new_prompt = _first_genuine_user_text(new_rows)

    report = {
        "rows_before": len(rows),
        "rows_after": len(new_rows),
        "blocks_split": split_count,
        "token_total_before": old_total,
        "token_total_after": new_total,
        "token_delta": new_total - old_total,
        "char_total_before": sum(r["char_count"] for r in rows),
        "char_total_after": sum(r["char_count"] for r in new_rows),
        "prompt_before": old_prompt,
        "prompt_after": new_prompt,
        "prompt_changed": changed and old_prompt != new_prompt,
    }
    return changed, new_rows, report


# ---------------------------------------------------------------------------
# Backend adapters -- each reuses that store module's own primitives
# ---------------------------------------------------------------------------


class _SqliteAdapter:
    name = "sqlite"

    def __init__(self):
        from metrics import store_sqlite

        self._store = store_sqlite

    def session_ids(self):
        conn = self._store._connect()
        try:
            rows = conn.execute("SELECT session_id FROM sessions ORDER BY session_id").fetchall()
        finally:
            conn.close()
        return [r["session_id"] for r in rows]

    def load_blocks(self, session_id):
        conn = self._store._connect()
        try:
            rows = conn.execute(
                "SELECT category, label, char_count, token_estimate, turn_n, status, content "
                "FROM context_blocks WHERE session_id=? ORDER BY seq",
                (session_id,),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def write_session(self, session_id, new_rows, new_prompt):
        """One SQLite transaction per session: delete this session's
        context_blocks, rewrite them densely renumbered, update
        sessions.prompt."""
        conn = self._store._connect()
        try:
            conn.execute("BEGIN")
            conn.execute("DELETE FROM context_blocks WHERE session_id=?", (session_id,))
            for seq, r in enumerate(new_rows):
                conn.execute(
                    "INSERT INTO context_blocks VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        session_id,
                        seq,
                        r["category"],
                        r["label"],
                        r["char_count"],
                        r["token_estimate"],
                        r["turn_n"],
                        r.get("status"),
                        r.get("content"),
                    ),
                )
            if new_prompt is not None:
                conn.execute(
                    "UPDATE sessions SET prompt=? WHERE session_id=?",
                    (new_prompt, session_id),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


class _DynamoAdapter:
    name = "dynamodb"

    def __init__(self):
        from metrics import store_dynamodb

        self._store = store_dynamodb

    def session_ids(self):
        items = self._store._scan_all(
            FilterExpression="sk = :sk",
            ExpressionAttributeValues={":sk": "SESSION"},
        )
        return sorted(i["session_id"] for i in items)

    def load_blocks(self, session_id):
        from mci_common.dynamo import clean_decimal as _clean

        resp = self._store._table.query(
            KeyConditionExpression="session_id = :sid AND begins_with(sk, :prefix)",
            ExpressionAttributeValues={":sid": session_id, ":prefix": "CTXBLOCK#"},
        )
        items = sorted(_clean(resp.get("Items", [])), key=lambda i: i["sk"])
        return [
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
        ]

    def write_session(self, session_id, new_rows, new_prompt):
        """Delete-then-put over this session's CTXBLOCK# items via a
        single batch_writer (same non-transactional tradeoff
        record_session already accepts on this backend). Idempotence
        covers a partial failure: re-running resumes safely."""
        existing = self._store._table.query(
            KeyConditionExpression="session_id = :sid AND begins_with(sk, :prefix)",
            ExpressionAttributeValues={":sid": session_id, ":prefix": "CTXBLOCK#"},
        ).get("Items", [])
        with self._store._table.batch_writer() as batch:
            for it in existing:
                batch.delete_item(Key={"session_id": session_id, "sk": it["sk"]})
            for seq, r in enumerate(new_rows):
                item = {
                    "session_id": session_id,
                    "sk": f"CTXBLOCK#{seq:04d}",
                    "category": r["category"],
                    "label": r["label"],
                    "char_count": r["char_count"],
                    "token_estimate": r["token_estimate"],
                    "turn_n": r["turn_n"],
                }
                if r.get("status") is not None:
                    item["status"] = r["status"]
                if r.get("content") is not None:
                    item["content"] = r["content"]
                batch.put_item(Item=item)
        if new_prompt is not None:
            self._store._table.update_item(
                Key={"session_id": session_id, "sk": "SESSION"},
                UpdateExpression="SET prompt = :p",
                ExpressionAttributeValues={":p": new_prompt},
            )


class _FirestoreAdapter:
    name = "firestore"

    def __init__(self):
        from metrics import store_firestore

        self._store = store_firestore

    def session_ids(self):
        client = self._store._client()
        return sorted(d.id for d in self._store._sessions(client).stream())

    def load_blocks(self, session_id):
        client = self._store._client()
        docs = (
            self._store._sessions(client)
            .document(session_id)
            .collection("context_blocks")
            .order_by("_seq")
            .stream()
        )
        rows = []
        for d in docs:
            data = d.to_dict()
            rows.append(
                {
                    "category": data["category"],
                    "label": data["label"],
                    "char_count": data["char_count"],
                    "token_estimate": data["token_estimate"],
                    "turn_n": data.get("turn_n"),
                    "status": data.get("status"),
                    "content": data.get("content"),
                }
            )
        return rows

    def write_session(self, session_id, new_rows, new_prompt):
        """Firestore batched write: all deletes + all sets + the prompt
        update commit atomically (<=500 ops, far above any real
        session's block count)."""
        client = self._store._client()
        session_ref = self._store._sessions(client).document(session_id)
        blocks_ref = session_ref.collection("context_blocks")

        batch = client.batch()
        for d in blocks_ref.stream():
            batch.delete(d.reference)
        for seq, r in enumerate(new_rows):
            batch.set(
                blocks_ref.document(str(seq).zfill(self._store._SEQ_WIDTH)),
                {
                    "_seq": seq,
                    "category": r["category"],
                    "label": r["label"],
                    "char_count": r["char_count"],
                    "token_estimate": r["token_estimate"],
                    "turn_n": r["turn_n"],
                    "status": r.get("status"),
                    "content": r.get("content"),
                },
            )
        if new_prompt is not None:
            batch.update(session_ref, {"prompt": new_prompt})
        batch.commit()


def _make_adapter():
    backend = os.environ.get("STORAGE_BACKEND", "sqlite")
    if backend == "dynamodb":
        return _DynamoAdapter()
    if backend == "firestore":
        return _FirestoreAdapter()
    return _SqliteAdapter()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_session_diff(session_id, report):
    print(f"\nsession {session_id}")
    print(
        f"  rows:   {report['rows_before']} -> {report['rows_after']}  "
        f"({report['blocks_split']} block(s) split)"
    )
    print(
        f"  tokens: {report['token_total_before']} -> {report['token_total_after']}  "
        f"(delta {report['token_delta']:+d})   [MUST be 0]"
    )
    print(
        f"  chars:  {report['char_total_before']} -> {report['char_total_after']}  "
        f"(delta {report['char_total_after'] - report['char_total_before']:+d})"
    )
    if report["prompt_changed"]:
        before = (report["prompt_before"] or "")[:80].replace("\n", " ")
        after = (report["prompt_after"] or "")[:80].replace("\n", " ")
        print(f"  prompt: {before!r}")
        print(f"       -> {after!r}")


def run(apply=False, only_session=None):
    adapter = _make_adapter()
    print(f"backend: {adapter.name}   mode: {'APPLY' if apply else 'dry-run'}")

    session_ids = [only_session] if only_session else adapter.session_ids()
    if only_session:
        print(f"restricted to session {only_session!r}")

    touched = 0
    total_blocks_split = 0
    for sid in session_ids:
        rows = adapter.load_blocks(sid)
        if not rows:
            continue
        changed, new_rows, report = _plan_session(rows)
        if not changed:
            continue

        # Hard invariants: totals must not move. Refuse to write otherwise.
        assert report["token_delta"] == 0, (
            f"session {sid}: token total moved by {report['token_delta']} "
            f"({report['token_total_before']} -> {report['token_total_after']}); refusing to write"
        )
        assert report["char_total_after"] == report["char_total_before"], (
            f"session {sid}: char total moved; refusing to write"
        )

        touched += 1
        total_blocks_split += report["blocks_split"]
        _print_session_diff(sid, report)

        if apply:
            new_prompt = report["prompt_after"] if report["prompt_changed"] else None
            adapter.write_session(sid, new_rows, new_prompt)

    print(
        f"\n{'applied to' if apply else 'would touch'} {touched} session(s), "
        f"{total_blocks_split} block(s) split. Per-session token delta 0 (asserted)."
    )
    if not apply and touched:
        print("re-run with --apply to write these changes.")
    return touched


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform the writes (default is a dry run that touches nothing)",
    )
    parser.add_argument(
        "--session",
        metavar="ID",
        help="migrate only this session_id (for testing)",
    )
    args = parser.parse_args(argv)
    run(apply=args.apply, only_session=args.session)
    return 0


if __name__ == "__main__":
    sys.exit(main())
