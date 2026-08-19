"""Pins the shape of the deterministic demo dataset the public Cloud Run
image bakes in (see scripts/seed_demo_db.py, Dockerfile) — a fresh-eyes
regression test that this stays: (a) reproducible byte-for-byte, since
that's the entire point of avoiding uuid4()/time.time(), and (b) rich
enough for the Context Window Explorer / Recent Sessions demo to look
real (mixed models, at least one failed tool call, at least one
turn-limit trace, non-empty context_blocks everywhere).
"""

import sqlite3

from scripts.seed_demo_db import build_sessions, seed


def test_seed_is_deterministic(tmp_path):
    out = tmp_path / "demo.db"
    seed(out)
    first = out.read_bytes()
    seed(out)
    second = out.read_bytes()
    assert first == second


def test_seed_shape(tmp_path):
    out = tmp_path / "demo.db"
    seed(out)
    conn = sqlite3.connect(out)
    conn.row_factory = sqlite3.Row

    sessions = conn.execute("SELECT * FROM sessions").fetchall()
    assert len(sessions) == len(build_sessions()) == 12

    models = {row["model"] for row in sessions}
    assert len(models) >= 2  # mixed models

    failed = conn.execute("SELECT COUNT(*) c FROM tool_calls WHERE status='error'").fetchone()["c"]
    assert failed > 0

    turn_limit_answer = conn.execute(
        "SELECT prompt FROM sessions WHERE session_id='demo-session-12'"
    ).fetchone()
    assert turn_limit_answer is not None

    for row in sessions:
        blocks = conn.execute(
            "SELECT COUNT(*) c FROM context_blocks WHERE session_id=?", (row["session_id"],)
        ).fetchone()["c"]
        assert blocks > 0, f"{row['session_id']} has no context_blocks"
