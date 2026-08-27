"""Tests for mcp_server.otlp.common.split_injected_context and its
byte-exact reconstruction / proportional token-split contract. This is
the ONE shared function the reclassification migration and the future
claude_code.py mapper both import, so its invariants are pinned hard
here.
"""

from mcp_server.otlp.common import (
    CATEGORY_ANSWER,
    CATEGORY_COMMAND,
    CATEGORY_INJECTED,
    CATEGORY_USER,
    contains_injected_wrappers,
    distribute_int,
    distribute_token_estimate,
    estimate_tokens,
    split_injected_context,
)

_SR = "<system-reminder>\nCLAUDE.md says be terse.\nToday is 2026-08-27.\n</system-reminder>"
_SR2 = "<system-reminder>Available skills: design, dataviz.</system-reminder>"
_SR_DEFERRED = "<system-reminder>\nDeferred tools are now available.\n</system-reminder>"
_CMD_GROUP = (
    "            <command-name>/deploy</command-name>\n"
    "            <command-message>deploy is running</command-message>\n"
    "            <command-args>--prod</command-args>"
)
_IDE = "<ide_opened_file>The user opened webapp/app.py</ide_opened_file>"
_SESSION = "<session>the user's typed words live here</session>"
_FORK = "<fork-boilerplate>You are a fork. Inherit context.</fork-boilerplate>"
_TASK_NOTIF = (
    "<task-notification>\nAgent done.\n<result>body has a literal </result> inside"
    "</result>\n<usage>subagent_tokens: 1234</usage>\n</task-notification>"
)


def _recon(frags):
    return "".join(f for f, _ in frags)


# --------------------------------------------------------------------------- #
# pure / single-fragment cases
# --------------------------------------------------------------------------- #


def test_pure_injected_returns_one_injected_block():
    assert split_injected_context(_SR, CATEGORY_USER) == [(_SR, CATEGORY_INJECTED)]


def test_no_wrappers_returns_single_base_fragment():
    text = "just an ordinary user message, no wrappers here"
    assert split_injected_context(text, CATEGORY_USER) == [(text, CATEGORY_USER)]


def test_empty_and_none_return_empty_list():
    assert split_injected_context("", CATEGORY_USER) == []
    assert split_injected_context(None, CATEGORY_USER) == []


# --------------------------------------------------------------------------- #
# leading wrapper + prose  ->  [injected|command, base]
# --------------------------------------------------------------------------- #


def test_injected_plus_prose_returns_injected_then_user():
    text = _SR + "\n\nPlease refactor the parser."
    frags = split_injected_context(text, CATEGORY_USER)
    assert frags == [
        (_SR + "\n\n", CATEGORY_INJECTED),  # separator kept ON the injected side
        ("Please refactor the parser.", CATEGORY_USER),
    ]
    assert _recon(frags) == text


def test_command_group_is_command_category_and_keeps_indentation():
    text = _CMD_GROUP + "\n\ntyped prompt"
    frags = split_injected_context(text, CATEGORY_USER)
    assert frags[0][1] == CATEGORY_COMMAND
    assert frags[1] == ("typed prompt", CATEGORY_USER)
    assert _recon(frags) == text


def test_ide_opened_file_and_fork_boilerplate_are_injected():
    for wrapper in (_IDE, _FORK):
        text = wrapper + "\n\nYour directive: do the thing."
        frags = split_injected_context(text, CATEGORY_USER)
        assert frags[0][1] == CATEGORY_INJECTED
        assert frags[1] == ("Your directive: do the thing.", CATEGORY_USER)
        assert _recon(frags) == text


def test_session_wrapper_tagged_injected_documented_mislabel():
    text = _SESSION + "\n\nrest of prompt"
    frags = split_injected_context(text, CATEGORY_USER)
    # whole <session> span is injected (accepted minor mislabel of the
    # inner user text -- see docstring)
    assert frags[0] == (_SESSION + "\n\n", CATEGORY_INJECTED)
    assert frags[1] == ("rest of prompt", CATEGORY_USER)


def test_multiple_leading_wrappers_collapse_into_one_run():
    text = _SR + "\n\n" + _SR2 + "\n\nactual question?"
    frags = split_injected_context(text, CATEGORY_USER)
    assert len(frags) == 2
    assert frags[0][1] == CATEGORY_INJECTED
    assert frags[1] == ("actual question?", CATEGORY_USER)
    assert _recon(frags) == text


def test_task_notification_matched_to_last_close_tag():
    text = _TASK_NOTIF + "\n\ncontinue please"
    frags = split_injected_context(text, CATEGORY_USER)
    assert frags[0][0] == _TASK_NOTIF + "\n\n"
    assert frags[0][1] == CATEGORY_INJECTED
    assert frags[1] == ("continue please", CATEGORY_USER)
    assert _recon(frags) == text


# --------------------------------------------------------------------------- #
# assistant-side trailing wrapper  ->  [answer, injected]
# --------------------------------------------------------------------------- #


def test_assistant_side_trailing_injected_is_injected_not_answer():
    text = "Here is the completed refactor." + "\n\n" + _SR_DEFERRED
    frags = split_injected_context(text, CATEGORY_ANSWER)
    assert frags == [
        ("Here is the completed refactor.", CATEGORY_ANSWER),
        ("\n\n" + _SR_DEFERRED, CATEGORY_INJECTED),  # separator kept ON injected
    ]
    assert _recon(frags) == text
    assert all(c != CATEGORY_ANSWER for f, c in frags if _SR_DEFERRED in f)


# --------------------------------------------------------------------------- #
# boundary anchoring  -- backticked mentions must NOT be peeled
# --------------------------------------------------------------------------- #


def test_mid_sentence_backticked_tag_is_not_reclassified():
    text = "The mapper strips `<system-reminder>` and `<command-name>` wrappers."
    assert split_injected_context(text, CATEGORY_USER) == [(text, CATEGORY_USER)]
    assert contains_injected_wrappers(text, CATEGORY_USER) is False


def test_leading_wrapper_without_canonical_double_newline_is_left_alone():
    # single newline, not the canonical "\n\n" separator -> not a clean
    # harness boundary -> whole string stays user
    text = _SR + "\nnot a real boundary"
    assert split_injected_context(text, CATEGORY_USER) == [(text, CATEGORY_USER)]


def test_wrapper_name_quoted_at_string_start_is_not_a_wrapper():
    text = "`<system-reminder>` is the tag name.\n\nrest"
    assert split_injected_context(text, CATEGORY_USER) == [(text, CATEGORY_USER)]


# --------------------------------------------------------------------------- #
# byte-exact reconstruction across a realistic mixed prompt
# --------------------------------------------------------------------------- #


def test_reconstruction_byte_exact_realistic_prompt():
    text = _CMD_GROUP + "\n\nShip the release once tests are green."
    frags = split_injected_context(text, CATEGORY_USER)
    assert _recon(frags) == text
    assert sum(len(f) for f, _ in frags) == len(text)


def test_multiline_system_reminder_trailing_newline_counts():
    # the "\n" before </system-reminder> is part of the wrapper body and
    # must not be stripped when the fragment is sized
    sr = "<system-reminder>\n\nbody\n\n</system-reminder>"
    text = sr + "\n\nafter"
    frags = split_injected_context(text, CATEGORY_USER)
    assert frags[0][0] == sr + "\n\n"
    assert _recon(frags) == text


# --------------------------------------------------------------------------- #
# proportional token distribution
# --------------------------------------------------------------------------- #


def test_distribute_token_estimate_sums_exactly():
    assert sum(distribute_token_estimate([10, 90], 25)) == 25
    assert sum(distribute_token_estimate([1, 1, 1], 7)) == 7
    assert sum(distribute_token_estimate([500, 3], 126)) == 126
    # remainder lands on the last fragment
    assert distribute_token_estimate([1, 1, 1], 7) == [2, 2, 3]


def test_distribute_int_degenerate_zero_chars():
    assert distribute_int([0, 0], 5) == [0, 5]
    assert distribute_int([], 5) == []


def test_token_split_beats_naive_reestimate_for_total_preservation():
    # whole-string estimate, then split by char proportion, must equal
    # the whole exactly -- naive per-fragment estimate would not.
    text = _SR + "\n\n" + "x" * 137
    frags = split_injected_context(text, CATEGORY_USER)
    whole = estimate_tokens(text)
    split = distribute_token_estimate([len(f) for f, _ in frags], whole)
    assert sum(split) == whole
    naive = sum(estimate_tokens(f) for f, _ in frags)
    # the naive sum is allowed to differ; that's the whole point
    assert isinstance(naive, int)


# --------------------------------------------------------------------------- #
# idempotence
# --------------------------------------------------------------------------- #


def test_carved_fragments_are_idempotent():
    text = _CMD_GROUP + "\n\n" + "real prompt body here"
    frags = split_injected_context(text, CATEGORY_USER)
    for frag_text, category in frags:
        if category == CATEGORY_USER:
            assert contains_injected_wrappers(frag_text, CATEGORY_USER) is False
        else:
            # re-splitting an already-carved wrapper fragment is a fixpoint
            again = split_injected_context(frag_text, CATEGORY_USER)
            assert again == [(frag_text, category)]


def test_contains_injected_wrappers_only_true_at_clean_boundary():
    assert contains_injected_wrappers(_SR + "\n\nx", CATEGORY_USER) is True
    assert contains_injected_wrappers("x\n\n" + _SR_DEFERRED, CATEGORY_ANSWER) is True
    assert contains_injected_wrappers(_SR, CATEGORY_USER) is True
    assert contains_injected_wrappers("mentions `<session>` inline", CATEGORY_USER) is False
    assert contains_injected_wrappers("", CATEGORY_USER) is False
    assert contains_injected_wrappers(None, CATEGORY_USER) is False
