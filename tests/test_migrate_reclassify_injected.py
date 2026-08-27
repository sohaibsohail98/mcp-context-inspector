"""Migration test for scripts/migrate_reclassify_injected.py against an
isolated sqlite DB (the tests/conftest.py `isolated_sqlite_db` fixture).

Seeds context_blocks that were mislabelled under the old blanket
user/answer rule, runs the migration, and asserts:
  * categories / labels are fixed (injected / command split out),
  * seq order is preserved and densely renumbered,
  * sessions.prompt is recomputed from the first genuine user block,
  * per-session total token_estimate is UNCHANGED (the hard invariant),
  * byte-exact reconstruction of every split row's original content,
  * a second --apply run is a no-op (idempotence).
"""


import pytest

from mcp_server.otlp.common import estimate_tokens
from scripts import migrate_reclassify_injected as mig

_SR = "<system-reminder>\nCLAUDE.md: be terse.\nToday is 2026-08-27.\n</system-reminder>"
_CMD = (
    "<command-name>/review</command-name>\n"
    "<command-message>review is running</command-message>\n"
    "<command-args>--fast</command-args>"
)
_SR_DEFERRED = "<system-reminder>\nDeferred tools are now available via ToolSearch.\n</system-reminder>"


def _seed_block(conn, session_id, seq, category, label, content, turn_n=0, status=None):
    """Insert a context_blocks row whose char_count / token_estimate are
    computed from `content` the way the mapper did at ingest time."""
    conn.execute(
        "INSERT INTO context_blocks VALUES (?,?,?,?,?,?,?,?,?)",
        (
            session_id,
            seq,
            category,
            label,
            len(content),
            estimate_tokens(content),
            turn_n,
            status,
            content,
        ),
    )


def _rows(conn, session_id):
    cur = conn.execute(
        "SELECT seq, category, label, char_count, token_estimate, turn_n, status, content "
        "FROM context_blocks WHERE session_id=? ORDER BY seq",
        (session_id,),
    )
    return [dict(r) for r in cur.fetchall()]


@pytest.fixture
def seeded(isolated_sqlite_db):
    store = isolated_sqlite_db
    conn = store._connect()

    # session A: first "user" block is really a <system-reminder> + typed
    # prompt; a later clean user turn; an assistant turn with a trailing
    # deferred-tools system-reminder.
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("sessA", "WRONG: <system-reminder>...", "claude-x", 0, 0, 0, 0, 0, 0.0, 1.0, None, "claude_code", "closed"),
    )
    _seed_block(conn, "sessA", 0, "system", "System prompt", "You are a helpful agent.", turn_n=None)
    _seed_block(conn, "sessA", 1, "user", "User message", _SR + "\n\nRefactor the OTLP mapper, please.")
    _seed_block(conn, "sessA", 2, "answer", "Assistant response", "Done. Here is the refactor.\n\n" + _SR_DEFERRED)
    _seed_block(conn, "sessA", 3, "user", "User message", "Now add a test.", turn_n=1)

    # session B: a slash-command invocation -- the whole first user block
    # is the <command-*> group + typed prose.
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("sessB", "WRONG", "claude-x", 0, 0, 0, 0, 0, 0.0, 2.0, None, "claude_code", "closed"),
    )
    _seed_block(conn, "sessB", 0, "user", "User message", _CMD + "\n\nSummarise the diff.")

    # session C: nothing to migrate (plain prose, plus a backticked
    # mention that must NOT be peeled).
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("sessC", "Fix the parser bug", "claude-x", 0, 0, 0, 0, 0, 0.0, 3.0, None, "claude_code", "closed"),
    )
    _seed_block(conn, "sessC", 0, "user", "User message", "Fix the parser bug")
    _seed_block(conn, "sessC", 1, "answer", "Assistant response", "The mapper strips `<system-reminder>` wrappers.")

    conn.commit()
    conn.close()
    return store


def _token_total(conn, session_id):
    return conn.execute(
        "SELECT COALESCE(SUM(token_estimate),0) FROM context_blocks WHERE session_id=?",
        (session_id,),
    ).fetchone()[0]


def _char_total(conn, session_id):
    return conn.execute(
        "SELECT COALESCE(SUM(char_count),0) FROM context_blocks WHERE session_id=?",
        (session_id,),
    ).fetchone()[0]


def test_dry_run_touches_nothing(seeded, capsys):
    store = seeded
    conn = store._connect()
    before = {s: _rows(conn, s) for s in ("sessA", "sessB", "sessC")}
    conn.close()

    mig.run(apply=False)
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "sessA" in out and "sessB" in out

    conn = store._connect()
    after = {s: _rows(conn, s) for s in ("sessA", "sessB", "sessC")}
    conn.close()
    assert before == after  # dry run wrote nothing


def test_apply_fixes_categories_labels_seq_and_prompt(seeded):
    store = seeded
    conn = store._connect()
    tok_before = {s: _token_total(conn, s) for s in ("sessA", "sessB", "sessC")}
    char_before = {s: _char_total(conn, s) for s in ("sessA", "sessB", "sessC")}
    orig_a1 = _rows(conn, "sessA")[1]["content"]
    orig_b0 = _rows(conn, "sessB")[0]["content"]
    conn.close()

    mig.run(apply=True)

    conn = store._connect()
    rows_a = _rows(conn, "sessA")
    rows_b = _rows(conn, "sessB")
    rows_c = _rows(conn, "sessC")

    # --- session A ---
    # row 1 (user + system-reminder) split into injected then user;
    # row 2 (answer + trailing deferred sr) split into answer then injected.
    cats_a = [(r["category"], r["label"]) for r in rows_a]
    assert cats_a == [
        ("system", "System prompt"),
        ("injected", "Injected context"),
        ("user", "User message"),
        ("answer", "Assistant response"),
        ("injected", "Injected context"),
        ("user", "User message"),
    ]
    # seq densely renumbered 0..N-1 in order
    assert [r["seq"] for r in rows_a] == list(range(len(rows_a)))
    # byte-exact reconstruction of the original row 1 content
    inj, usr = rows_a[1], rows_a[2]
    assert inj["content"] + usr["content"] == orig_a1
    assert inj["content"].endswith("\n\n")  # canonical separator kept on injected side
    # answer/injected split of original row 2
    ans, inj2 = rows_a[3], rows_a[4]
    assert ans["content"] + inj2["content"] == "Done. Here is the refactor.\n\n" + _SR_DEFERRED
    # turn_n carried onto every fragment
    assert usr["turn_n"] == 0 and rows_a[5]["turn_n"] == 1

    # prompt recomputed from the first GENUINE user block (row 2's text)
    prompt_a = conn.execute("SELECT prompt FROM sessions WHERE session_id='sessA'").fetchone()[0]
    assert prompt_a == "Refactor the OTLP mapper, please."

    # --- session B ---
    cats_b = [(r["category"], r["label"]) for r in rows_b]
    assert cats_b == [("command", "Slash command"), ("user", "User message")]
    assert rows_b[0]["content"] + rows_b[1]["content"] == orig_b0
    prompt_b = conn.execute("SELECT prompt FROM sessions WHERE session_id='sessB'").fetchone()[0]
    assert prompt_b == "Summarise the diff."

    # --- session C: untouched ---
    assert [(r["category"], r["content"]) for r in rows_c] == [
        ("user", "Fix the parser bug"),
        ("answer", "The mapper strips `<system-reminder>` wrappers."),
    ]

    # --- HARD INVARIANT: per-session token + char totals unchanged ---
    for s in ("sessA", "sessB", "sessC"):
        assert _token_total(conn, s) == tok_before[s], s
        assert _char_total(conn, s) == char_before[s], s
    conn.close()


def test_apply_is_idempotent(seeded):
    store = seeded
    mig.run(apply=True)

    conn = store._connect()
    first = {s: _rows(conn, s) for s in ("sessA", "sessB", "sessC")}
    prompts_first = dict(
        conn.execute("SELECT session_id, prompt FROM sessions").fetchall()
    )
    conn.close()

    touched_2nd = mig.run(apply=True)
    assert touched_2nd == 0  # nothing left to change

    conn = store._connect()
    second = {s: _rows(conn, s) for s in ("sessA", "sessB", "sessC")}
    prompts_second = dict(
        conn.execute("SELECT session_id, prompt FROM sessions").fetchall()
    )
    conn.close()
    assert first == second
    assert prompts_first == prompts_second


def test_single_session_flag_restricts_scope(seeded):
    store = seeded
    mig.run(apply=True, only_session="sessB")

    conn = store._connect()
    # sessB migrated
    assert [r["category"] for r in _rows(conn, "sessB")] == ["command", "user"]
    # sessA left alone
    assert [r["category"] for r in _rows(conn, "sessA")] == ["system", "user", "answer", "user"]
    conn.close()


def test_token_totals_reported_and_zero_delta(seeded, capsys):
    mig.run(apply=False)
    out = capsys.readouterr().out
    # the dry-run prints a per-session token line with a delta that must be 0
    assert "delta +0" in out
    assert "MUST be 0" in out
