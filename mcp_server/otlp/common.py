"""Shared helpers for the per-vendor OTLP mappers: OTLP JSON wire-format
parsing (protobuf-JSON encoding, not the binary protobuf form) and the
chars-per-token fallback estimate. Both mappers import from here rather
than duplicating this parsing.
"""

import re

from mci_common.config import CHARS_PER_TOKEN_ESTIMATE

# The context_blocks categories the dashboard's CATEGORY_COLORS map
# and mci_common/timeline.py both expect. Every mapper must emit one of
# these, never an invented category string, or the block silently fails
# to render/color in the dashboard.
CATEGORY_SYSTEM = "system"
CATEGORY_TOOLS = "tools"
CATEGORY_USER = "user"
CATEGORY_REASONING = "reasoning"
CATEGORY_TOOL_CALL = "tool_call"
CATEGORY_TOOL_RESULT = "tool_result"
CATEGORY_ANSWER = "answer"

# Two new categories for harness-injected context that Claude Code
# prepends to (or appends to) a message string before the model ever
# sees it. These are NOT user- or assistant-authored, so labelling them
# "user"/"answer" (the old blanket rule) overstated how much of the
# context window the human actually drove.
CATEGORY_INJECTED = "injected"  # dashboard label "Injected context"
CATEGORY_COMMAND = "command"  # dashboard label "Slash command"

CATEGORY_LABELS = {
    CATEGORY_INJECTED: "Injected context",
    CATEGORY_COMMAND: "Slash command",
}


# ---------------------------------------------------------------------------
# Harness-injected context splitting
# ---------------------------------------------------------------------------
#
# Claude Code wraps machine-generated context around a message's real
# text before it is sent to the Anthropic Messages API. A transcript
# inventory of this repo's own sessions found nine wrapper families in
# two classes:
#
#   injected (CATEGORY_INJECTED)
#     <system-reminder>...</system-reminder>      (user AND assistant side)
#     <ide_opened_file>...</ide_opened_file>
#     <session>...</session>                      + its trailing title instructions
#     <fork-boilerplate>...</fork-boilerplate>
#     <task-notification>...</task-notification>  (frame + plumbing children)
#     <user-prompt-submit-hook>...</user-prompt-submit-hook>
#
#   command (CATEGORY_COMMAND)
#     <local-command-caveat>...</local-command-caveat>
#     <command-name>/<command-message>/<command-args>/<command-contents>
#     <local-command-stdout>/<local-command-stderr>
#     <bash-input>/<bash-stdout>/<bash-stderr>
#
# HARD RULES from the inventory (these are correctness, not style):
#
#  * ANCHOR ON A FRAGMENT BOUNDARY, NOT A BARE SUBSTRING. Every one of
#    these tag names also appears constantly quoted in backticks inside
#    genuine prose in this repo's transcripts. A wrapper is only peeled
#    when the fragment IS exactly the wrapper, OR the fragment STARTS
#    with `<wrapper>...</wrapper>` immediately followed by exactly "\n\n",
#    OR the fragment ENDS with "\n\n<wrapper>...</wrapper>" (the
#    assistant-side deferred-tools notice). A mid-sentence mention is
#    never reclassified.
#
#  * THE SEPARATOR IS ALWAYS EXACTLY "\n\n" (two newlines -- never
#    spaces, never one or three) and it is assigned to the INJECTED
#    side, so `original == injected_part + prose_part` (leading case) or
#    `original == prose_part + injected_part` (trailing case) is
#    byte-exact. No separator char is dropped or stored nowhere.
#
#  * AT MOST TWO PARTS, FIXED ORDER: `[injected][user]` for a user turn,
#    `[answer][injected]` for an assistant turn. Never three-way. The
#    <command-*> group is always its own synthetic message, separate
#    from the typed human prompt, so command + injected + typed prose
#    never co-occur in one string.
#
# Regex notes: <command-*>/<bash-*> sub-tag lines are indented (~12
# spaces) so the leading-run scanner tolerates leading whitespace and
# does not `^`-anchor each sub-tag. <task-notification> nests and its
# <usage> children use underscores (subagent_tokens), and a <result>
# body can itself contain a literal "</result>", so the notification is
# matched to its LAST "</task-notification>" and its children are NOT
# sub-split for accounting. Wrapper bodies are never `.strip()`ed when
# sized -- a trailing "\n" before "</system-reminder>" counts.

# Injected-class wrappers. Order within the alternation does not matter
# (each is a distinct tag name); DOTALL because every one is routinely
# multi-line.
_INJECTED_TAGS = (
    "system-reminder",
    "ide_opened_file",
    "session",
    "fork-boilerplate",
    "user-prompt-submit-hook",
)
# task-notification handled separately: greedy to the LAST close tag so a
# literal "</task-notification>" inside a nested <result> body doesn't
# truncate it.
_TASK_NOTIFICATION_RE = r"<task-notification>.*</task-notification>"

_COMMAND_TAGS = (
    "local-command-caveat",
    "command-name",
    "command-message",
    "command-args",
    "command-contents",
    "local-command-stdout",
    "local-command-stderr",
    "bash-input",
    "bash-stdout",
    "bash-stderr",
)

# A single wrapper span: an open tag, a lazy body, and the SAME tag's
# close tag, for any of the injected- or command-class names; or the
# greedy task-notification (matched to its LAST close tag so a nested
# literal "</task-notification>" in a <result> body can't truncate it).
#
# The open/close pairing is done with an explicit
# `<name>...</name>|<name2>...</name2>|...` alternation rather than one
# backreferenced `(?P<name>...)...</(?P=name)>`, because this sub-pattern
# is embedded MULTIPLE times inside _LEADING_RUN_RE and Python's `re`
# forbids the same group name appearing twice in one compiled pattern.
# The per-name alternation has no named groups, so it nests freely.
_ALL_PAIRED_TAGS = _INJECTED_TAGS + _COMMAND_TAGS
_ONE_WRAPPER = (
    r"(?:"
    + "|".join(rf"<{re.escape(t)}>.*?</{re.escape(t)}>" for t in _ALL_PAIRED_TAGS)
    + r"|"
    + _TASK_NOTIFICATION_RE
    + r")"
)
_ONE_WRAPPER_RE = re.compile(_ONE_WRAPPER, re.DOTALL)
_COMMAND_TAG_SET = frozenset(_COMMAND_TAGS)
# Recognise which class a peeled run belongs to by scanning its open tags.
_OPEN_TAG_RE = re.compile(r"<([a-z][a-z0-9_-]*)>")

# A LEADING run: one-or-more wrapper spans, each separated from the next
# by nothing or whitespace, starting at string start, and the whole run
# followed by exactly "\n\n" then more (prose) OR the run IS the whole
# string. Leading whitespace before the first tag is tolerated (indented
# <command-*> groups).
_LEADING_RUN_RE = re.compile(
    r"\A\s*(?:" + _ONE_WRAPPER + r")(?:\s*(?:" + _ONE_WRAPPER + r"))*",
    re.DOTALL,
)
# A TRAILING wrapper: "\n\n" then exactly one wrapper span then end of
# string. Used for the assistant-side "deferred tools are available"
# system-reminder appended after the real answer.
_TRAILING_WRAPPER_RE = re.compile(
    r"\n\n(?:" + _ONE_WRAPPER + r")\Z",
    re.DOTALL,
)


def _run_is_all_command(run_text):
    """True if every wrapper span in a leading run is command-class, so
    the peeled block is tagged CATEGORY_COMMAND rather than
    CATEGORY_INJECTED. A mixed run (shouldn't occur per rule 4, but be
    safe) or an empty scan is treated as injected."""
    open_tags = _OPEN_TAG_RE.findall(run_text)
    if not open_tags:
        return False
    return all(t in _COMMAND_TAG_SET for t in open_tags)


def contains_injected_wrappers(text, base_category=CATEGORY_USER):
    """True iff split_injected_context would actually peel something off
    `text` -- i.e. there is a boundary-anchored leading run or trailing
    wrapper, NOT merely a backticked mention somewhere in the prose.
    The migration uses this as its idempotency guard: a row it already
    split has no peelable wrapper left."""
    if not text:
        return False
    frags = split_injected_context(text, base_category)
    if len(frags) > 1:
        return True
    return bool(frags) and frags[0][1] != base_category


def split_injected_context(text, base_category):
    """Split one message string into at most two ordered, byte-exact,
    non-overlapping fragments, each tagged with the category it belongs
    to. This is the SINGLE shared implementation imported by both the
    one-off reclassification migration
    (``scripts/migrate_reclassify_injected.py``) and the Claude Code
    OTLP mapper.

    Returns a list of ``(fragment_text, category)`` tuples:

      * ``[]`` if ``text`` is ``None`` or empty.
      * ``[(text, base_category)]`` if nothing is peelable -- no
        boundary-anchored wrapper. A mid-prose backticked mention of a
        tag name is NOT peelable and lands here unchanged.
      * ``[(wrapper_run, injected_or_command)]`` if the whole string IS a
        wrapper run and nothing else.
      * ``[(wrapper_run, injected_or_command), (prose, base_category)]``
        -- a LEADING run of harness wrappers (``<command-*>`` group,
        ``<system-reminder>``, ``<session>``, ``<fork-boilerplate>``,
        ``<ide_opened_file>``, ``<task-notification>``, ...) followed by
        exactly ``"\\n\\n"`` and then the real typed prose. The
        ``"\\n\\n"`` separator is kept ON the wrapper fragment.
      * ``[(prose, base_category), (wrapper, CATEGORY_INJECTED)]`` -- the
        assistant-side case: real answer text, then exactly
        ``"\\n\\n"`` then a single trailing ``<system-reminder>`` (the
        "deferred tools are available" notice). The ``"\\n\\n"`` stays on
        the injected fragment.

    ``injected_or_command`` is ``CATEGORY_COMMAND`` when every wrapper in
    the peeled leading run is command-class (``<command-*>``,
    ``<local-command-*>``, ``<bash-*>``), else ``CATEGORY_INJECTED``.

    CONTRACT / INVARIANTS (both callers depend on these):

    1. BYTE-EXACT RECONSTRUCTION. ``"".join(f for f, _ in
       split_injected_context(text, c)) == text`` for every input. The
       fragments are adjacent slices of the original; nothing is
       inserted, dropped, reordered, trimmed, or normalised. The only
       separator between a wrapper fragment and a prose fragment is the
       canonical ``"\\n\\n"``, and it is INCLUDED in the wrapper
       fragment's text (never stored nowhere).

    2. AT MOST TWO FRAGMENTS, FIXED ORDER. Never three-way. A user turn
       peels to ``[injected/command, user]``; an assistant turn peels to
       ``[answer, injected]``. Both fragments are non-empty.

    3. BOUNDARY ANCHORING. A wrapper is peeled only when it sits exactly
       at the start of the string (optionally after whitespace, for
       indented ``<command-*>`` groups) and is either the whole string
       or immediately followed by ``"\\n\\n"``; or when it sits exactly
       at the end of the string preceded by ``"\\n\\n"``. A wrapper tag
       quoted mid-sentence in genuine prose is left alone.

    4. TOKEN DISTRIBUTION IS THE CALLER'S JOB, PROPORTIONALLY. This
       function does NOT compute per-fragment ``token_estimate``.
       ``estimate_tokens(a) + estimate_tokens(b)`` differs from
       ``estimate_tokens(a + b)`` by a token or two at the cut, because
       ``estimate_tokens`` is ``max(1, len // CHARS_PER_TOKEN_ESTIMATE)``
       (integer floor division, floor of 1). To keep a session's total
       ``token_estimate`` -- and therefore the dashboard's
       ``cumulative_pct`` and per-category ``%`` totals -- IDENTICAL
       before and after a split, the caller must:
         a. take the ORIGINAL row's stored ``token_estimate`` as the
            whole (computed once, at ingest, from the full untruncated
            text);
         b. split it across the fragments by ``char_count`` proportion
            via ``distribute_token_estimate`` in this module, which
            dumps the rounding remainder on the LAST fragment so
            ``sum(sub.token_estimate) == whole.token_estimate`` EXACTLY.
       Fragments are NEVER independently re-estimated. ``char_count`` is
       likewise reconciled to the original row's stored ``char_count``
       (which can exceed ``len(content)`` when the stored content was
       redacted/truncated) with the remainder on the last fragment.

    5. IDEMPOTENCE. After a split, a prose fragment has no
       boundary-anchored wrapper left (``contains_injected_wrappers`` is
       False) and a wrapper fragment is already categorised
       injected/command (the migration skips it by category). Re-running
       the migration ``--apply`` is a no-op.

    ACCEPTED MINOR MISLABELS (documented, deliberate -- the alternative
    is sub-parsing a wrapper body, which is fragile and not worth it at
    this scale):

      * ``<session>...</session>``: the whole span is tagged
        ``injected``. The inventory notes the inner text is really
        user-authored, but it is a small, bounded amount and peeling the
        two ``<session>`` tags while keeping the middle as ``user``
        would make this a three-way split (violating rule 2). Tagged
        injected wholesale.
      * ``<task-notification>...</task-notification>``: the whole frame
        (including any ``<result>`` body that is really assistant
        output) is tagged ``injected``. Same reasoning.
    """
    if not text:
        return []

    # --- trailing single wrapper (assistant deferred-tools notice) ---
    tm = _TRAILING_WRAPPER_RE.search(text)
    if tm and tm.start() > 0:
        prose = text[: tm.start()]
        wrapper = text[tm.start() :]  # includes the leading "\n\n"
        if prose:
            return [(prose, base_category), (wrapper, CATEGORY_INJECTED)]

    # --- leading wrapper run ---
    lm = _LEADING_RUN_RE.match(text)
    if lm:
        run_end = lm.end()
        run_text = text[:run_end]
        rest = text[run_end:]
        run_category = CATEGORY_COMMAND if _run_is_all_command(run_text) else CATEGORY_INJECTED
        if not rest:
            # whole string is the wrapper run
            return [(run_text, run_category)]
        if rest.startswith("\n\n"):
            prose = rest[2:]
            if not prose:
                # wrapper run + trailing "\n\n" and nothing else: still a
                # single fragment (no empty prose fragment emitted).
                return [(run_text + "\n\n", run_category)]
            # A leading <session> wrapper's trailing text is the harness's
            # own title-generation instructions ("Write the title in the
            # predominant language of the session ..."), NOT user prose.
            # <session> only ever appears in that title-gen subagent, so
            # the whole string is injected -- splitting here would (and
            # did, on prod data) leave that instruction as the session's
            # "prompt".
            if run_text.startswith("<session>"):
                return [(text, CATEGORY_INJECTED)]
            # canonical separator: keep it ON the wrapper fragment
            return [(run_text + "\n\n", run_category), (prose, base_category)]
        # A leading wrapper NOT followed by the canonical "\n\n" is not a
        # clean harness boundary (e.g. a wrapper name backticked at the
        # very start of prose, or a non-canonical separator). Leave the
        # whole string as base_category rather than guess.

    return [(text, base_category)]


def distribute_token_estimate(char_counts, whole_token_estimate):
    """Split ``whole_token_estimate`` across N fragments in proportion to
    their ``char_counts``, dumping the rounding remainder on the LAST
    fragment so ``sum(result) == whole_token_estimate`` EXACTLY.

    This is how the reclassification migration keeps a session's total
    token estimate -- and the dashboard's ``cumulative_pct`` /
    per-category ``%`` -- unchanged when one context_block row becomes
    two: the fragments are never independently re-estimated (that would
    drift by a token or two per cut, see
    ``split_injected_context``'s contract), they just re-divide the
    original row's already-stored estimate.

    ``char_counts``: list of per-fragment char counts (must sum > 0).
    Returns a list of ints, same length, each >= 0, summing to
    ``whole_token_estimate``.
    """
    total_chars = sum(char_counts)
    if total_chars <= 0 or not char_counts:
        # degenerate: put it all on the last fragment
        return [0] * (len(char_counts) - 1) + [whole_token_estimate] if char_counts else []
    out = []
    running = 0
    for cc in char_counts[:-1]:
        share = whole_token_estimate * cc // total_chars
        out.append(share)
        running += share
    out.append(whole_token_estimate - running)  # remainder on the last
    return out


def distribute_int(char_counts, whole):
    """Same proportional-split-with-remainder-on-last as
    ``distribute_token_estimate``, for any integer total (used to
    reconcile ``char_count`` to the original row's stored value when the
    stored ``content`` was redacted/truncated and is shorter than the
    original text the count was taken from)."""
    return distribute_token_estimate(char_counts, whole)


def estimate_tokens(text):
    """Character-count fallback for content with no exact token count
    attached (see CHARS_PER_TOKEN_ESTIMATE's docstring in
    mci_common/config.py). Only use this when a real `usage` block
    isn't available for the content being sized."""
    if not text:
        return 0
    return max(1, len(text) // CHARS_PER_TOKEN_ESTIMATE)


# Caps how much raw block text (Context Explorer's expand-to-view
# feature) a single row stores. Well above any real system prompt or
# typical tool_result, but bounds a pathological case (e.g. a tool
# reading a huge file) from bloating a single row without limit. The
# token_estimate/char_count above are always computed from the FULL
# untruncated text, so cost/size numbers stay accurate even when the
# stored preview is capped.
_MAX_STORED_CONTENT_CHARS = 50_000


def truncate_content(text):
    """Caps text for storage in a context_block's `content` field. Does
    not affect char_count/token_estimate, which are always computed from
    the real, untruncated text before this runs."""
    if len(text) <= _MAX_STORED_CONTENT_CHARS:
        return text
    return text[:_MAX_STORED_CONTENT_CHARS] + f"\n\n[... truncated, {len(text)} chars total]"


# A real AnyValue payload nests at most a couple of levels deep
# (attribute -> kvlistValue -> nested attribute). A cap well above any
# real payload but far below Python's default recursion limit stops a
# maliciously/accidentally deeply-nested arrayValue/kvlistValue from
# forcing a RecursionError on every request that touches it.
_MAX_OTLP_VALUE_DEPTH = 20


def _otlp_value(value_obj, _depth=0):
    """An OTLP JSON `AnyValue` is `{"stringValue": ...}` or
    `{"intValue": ...}` or `{"boolValue": ...}` or `{"doubleValue": ...}`
    etc, with exactly one key present. Returns the unwrapped Python value.
    Unrecognized/empty AnyValue objects return None rather than raising,
    since a single malformed attribute shouldn't sink an entire batch."""
    if not value_obj or _depth >= _MAX_OTLP_VALUE_DEPTH:
        return None
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in value_obj:
            v = value_obj[key]
            return int(v) if key == "intValue" and isinstance(v, str) else v
    if "arrayValue" in value_obj:
        return [_otlp_value(v, _depth + 1) for v in value_obj["arrayValue"].get("values", [])]
    if "kvlistValue" in value_obj:
        return attrs_list_to_dict(value_obj["kvlistValue"].get("values", []), _depth + 1)
    return None


def attrs_list_to_dict(attr_list, _depth=0):
    """OTLP JSON represents attribute lists as
    `[{"key": "...", "value": {"stringValue": "..."}}, ...]`. Every
    resource/log/span/metric-datapoint attribute list in the wire format
    uses this exact shape. Converts to a plain `{key: value}` dict."""
    out = {}
    if _depth >= _MAX_OTLP_VALUE_DEPTH:
        return out
    for attr in attr_list or []:
        key = attr.get("key")
        if key is not None:
            out[key] = _otlp_value(attr.get("value", {}), _depth + 1)
    return out


def resource_attrs_dict(resource_obj):
    """resource_obj: the `"resource"` object on a resourceLogs/
    resourceMetrics/resourceSpans entry, `{"attributes": [...]}`."""
    return attrs_list_to_dict(resource_obj.get("attributes", []))


def log_record_body_text(log_record):
    """A LogRecord's `body` is itself an AnyValue, usually a
    stringValue carrying JSON (the raw Messages API body, for Claude
    Code's api_request_body/api_response_body events) or plain text.
    Returns the unwrapped value as-is (str, dict, or None). Callers
    that expect JSON should json.loads() a str result themselves and
    handle a decode failure explicitly rather than this helper guessing."""
    return _otlp_value(log_record.get("body", {}))
