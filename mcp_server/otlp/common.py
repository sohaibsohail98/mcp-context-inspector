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
#    genuine prose in this repo's transcripts. A wrapper run is only
#    peeled where it OPENS the string (optionally after whitespace) or
#    sits immediately after a "\n\n", AND it CLOSES the string or sits
#    immediately before a "\n\n". A mid-sentence mention is never
#    reclassified, and neither is a wrapper joined to prose by a single
#    "\n".
#
#  * THE SEPARATOR IS ALWAYS EXACTLY "\n\n" (two newlines -- never
#    spaces, never one or three) and it is assigned to the INJECTED
#    side, so reconstruction by simple concatenation is byte-exact. No
#    separator char is dropped or stored nowhere. A run with prose on
#    both sides takes one separator from each side, so every separator
#    is still accounted for exactly once.
#
#  * ANY NUMBER OF PARTS, ALTERNATING. A turn is NOT limited to
#    `[injected][user]` or `[answer][injected]`. Claude Code routinely
#    bundles reminders around and between real text in one logical turn
#    -- a <command-*> group, then the typed prompt, then another
#    <system-reminder> -- and capping the split at two parts is exactly
#    what left those bundled reminders wearing the enclosing message's
#    `user`/`answer` category, under-reporting `injected` app-wide.
#    Each run is classified from its OWN tags, independently.
#
# Regex notes: <command-*>/<bash-*> sub-tag lines are indented (~12
# spaces) so the run scanner tolerates leading whitespace and
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
# is embedded TWICE inside _RUN_RE and Python's `re` forbids the same
# group name appearing twice in one compiled pattern. The per-name
# alternation has no named groups, so it nests freely.
_ALL_PAIRED_TAGS = _INJECTED_TAGS + _COMMAND_TAGS
_ONE_WRAPPER = (
    r"(?:"
    + "|".join(rf"<{re.escape(t)}>.*?</{re.escape(t)}>" for t in _ALL_PAIRED_TAGS)
    + r"|"
    + _TASK_NOTIFICATION_RE
    + r")"
)
_COMMAND_TAG_SET = frozenset(_COMMAND_TAGS)
# Recognise which class a peeled run belongs to by scanning its open tags.
_OPEN_TAG_RE = re.compile(r"<([a-z][a-z0-9_-]*)>")

# A RUN: one-or-more wrapper spans, each separated from the next by
# nothing or whitespace. Anchored with `.match(text, offset)` at the
# candidate offsets _candidate_starts yields, never `.search`/
# `.finditer` -- see that function for why.
_RUN_RE = re.compile(
    r"(?:" + _ONE_WRAPPER + r")(?:\s*(?:" + _ONE_WRAPPER + r"))*",
    re.DOTALL,
)

# Leading whitespace before the first wrapper of a string is tolerated,
# as is horizontal indentation after the canonical "\n\n" separator
# (<command-*> groups arrive indented ~12 spaces).
_LEADING_WS_RE = re.compile(r"\s*")
_INDENT_RE = re.compile(r"[ \t]*")


def _candidate_starts(text):
    """Ordered, de-duplicated offsets at which a wrapper run may begin.

    A run is only recognised at the very start of the string (optionally
    after whitespace) or immediately after the canonical "\n\n"
    separator (optionally after horizontal indentation). Enumerating
    those offsets and matching `_RUN_RE` at each IS the boundary rule --
    a wrapper tag quoted mid-sentence never sits at one of them.

    It is also what keeps the scan linear. Matching the run pattern at
    every position (`finditer`) would, on a transcript containing an
    open "<system-reminder>" with no close tag, re-scan the rest of the
    string once per "<" in a 50k-char block; real transcripts are full
    of angle brackets. Candidate offsets are bounded by the number of
    blank lines instead.
    """
    offsets = []
    seen = set()

    def add(off):
        if 0 <= off <= len(text) and off not in seen:
            seen.add(off)
            offsets.append(off)

    add(0)
    add(_LEADING_WS_RE.match(text, 0).end())
    pos = text.find("\n\n")
    while pos != -1:
        after = pos + 2
        add(after)
        add(_INDENT_RE.match(text, after).end())
        pos = text.find("\n\n", pos + 1)
    offsets.sort()
    return offsets


def _wrapper_runs(text):
    """Find every boundary-delimited wrapper run in `text`, left to
    right and non-overlapping.

    Returns a list of ``(start, end, category)`` where ``start``/``end``
    already absorb the canonical ``"\n\n"`` separator on whichever side
    has real prose next to it, plus any leading/trailing whitespace-only
    remainder. So ``text[start:end]`` is exactly the fragment to emit,
    and the gaps between consecutive runs are exactly the prose
    fragments. A candidate that is not cleanly delimited on its closing
    side is skipped, not half-peeled.
    """
    runs = []
    cursor = 0
    for start in _candidate_starts(text):
        if start < cursor:
            continue
        match = _RUN_RE.match(text, start)
        if not match:
            continue
        end = match.end()
        rest = text[end:]
        if rest:
            if not rest.strip():
                # nothing but whitespace left: absorb it rather than
                # emit a whitespace-only prose fragment
                end = len(text)
            elif rest.startswith("\n\n"):
                end += 2  # canonical separator belongs to the wrapper side
            else:
                # not a clean closing boundary (e.g. a single "\n"):
                # leave this candidate alone rather than guess
                continue
        if text[:start].strip():
            start -= 2  # canonical separator belongs to the wrapper side
        else:
            start = 0  # only whitespace before it: the run opens the string
        runs.append((start, end, _run_category(match.group(0))))
        cursor = end
    return runs


def _run_category(run_text):
    return CATEGORY_COMMAND if _run_is_all_command(run_text) else CATEGORY_INJECTED


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
    """Split one message string into ordered, byte-exact,
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
      * otherwise, an ALTERNATING sequence of prose fragments
        (``base_category``) and wrapper-run fragments
        (``injected``/``command``), in the order they appear. A message
        that opens with harness wrappers starts with a wrapper fragment;
        one that ends with them (the assistant-side deferred-tools
        notice) ends with one; one with wrappers bundled BETWEEN two
        stretches of real prose yields all three, and so on with no
        fixed limit. Each run is categorised from its own tags, never
        from the enclosing message's role.

    ``injected_or_command`` is ``CATEGORY_COMMAND`` when every wrapper in
    a peeled run is command-class (``<command-*>``,
    ``<local-command-*>``, ``<bash-*>``), else ``CATEGORY_INJECTED``.

    CONTRACT / INVARIANTS (both callers depend on these):

    1. BYTE-EXACT RECONSTRUCTION. ``"".join(f for f, _ in
       split_injected_context(text, c)) == text`` for every input. The
       fragments are adjacent slices of the original; nothing is
       inserted, dropped, reordered, trimmed, or normalised. The only
       separator between a wrapper fragment and a prose fragment is the
       canonical ``"\n\n"``, and it is INCLUDED in the wrapper
       fragment's text (never stored nowhere). A run with prose on BOTH
       sides absorbs both its separators, one from each side, so each
       one is still accounted for exactly once.

    2. ALTERNATING, ANY LENGTH. Consecutive fragments never share a
       class: two wrapper runs separated only by whitespace are one run,
       so a wrapper fragment is always followed by a prose fragment and
       vice versa. Every fragment is non-empty. There is no cap on the
       count -- a turn whose reminders are bundled around the typed
       prompt genuinely does carry more than two parts, and collapsing
       them into two was what made bundled reminders inherit the
       message's ``user``/``answer`` category.

    3. BOUNDARY ANCHORING. A wrapper run is peeled only when it starts
       at the beginning of the string (optionally after whitespace, for
       indented ``<command-*>`` groups) or immediately after a canonical
       ``"\n\n"`` (again tolerating horizontal indentation), AND it
       ends at the end of the string or immediately before another
       ``"\n\n"``. A wrapper tag quoted mid-sentence in genuine prose,
       or one separated by a single ``"\n"``, is left alone.

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
       redacted/truncated) with the remainder on the last fragment. Both
       helpers take N fragments, not two.

    5. IDEMPOTENCE. After a split, a prose fragment has no
       boundary-anchored wrapper left (``contains_injected_wrappers`` is
       False) and a wrapper fragment is already categorised
       injected/command (the migration skips it by category). Re-running
       the migration ``--apply`` is a no-op.

    ACCEPTED MINOR MISLABELS (documented, deliberate -- the alternative
    is sub-parsing a wrapper body, which is fragile and not worth it at
    this scale):

      * ``<session>...</session>``: when the string OPENS with one, the
        whole string is tagged ``injected``. ``<session>`` only ever
        appears in the harness's title-generation subagent, where the
        text after ``</session>`` is that subagent's own title
        instruction, not user prose; peeling a ``user`` remainder here
        made that instruction the session's stored prompt on real prod
        data.
      * ``<task-notification>...</task-notification>``: the whole frame
        (including any ``<result>`` body that is really assistant
        output) is tagged ``injected``. Sub-parsing the body is not
        worth it.
    """
    if not text:
        return []

    runs = _wrapper_runs(text)
    if not runs:
        return [(text, base_category)]

    # A leading <session> run swallows the whole string (see ACCEPTED
    # MINOR MISLABELS above). lstrip: _wrapper_runs tolerates leading
    # whitespace, so an indented "<session>" still lands here.
    first_start, first_end, _ = runs[0]
    if first_start == 0 and text[:first_end].lstrip().startswith("<session>"):
        return [(text, CATEGORY_INJECTED)]

    frags = []
    cursor = 0
    for start, end, category in runs:
        if start > cursor:
            frags.append((text[cursor:start], base_category))
        frags.append((text[start:end], category))
        cursor = end
    if cursor < len(text):
        frags.append((text[cursor:], base_category))
    return frags


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
