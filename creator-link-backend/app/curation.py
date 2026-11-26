"""Character-range tracking for curation proposals.

A proposal targets ``[start, end)`` in the content of one immutable project
revision (positions are 0-based Unicode code points).  Accepting or withdrawing
it must happen on the *current* content, so the proposed range has to be
carried through every later revision.

Only curation accept/withdraw decisions record the exact span they changed
(``project_content_edits``).  The pre-existing direct PATCH endpoint replaces
the whole content string, so per the service contract an actual content change
there is treated as a whole-document rewrite and therefore conflicts with every
tracked range; a title-only or byte-identical save has no effect at all.

Mapping rules, applied per revision that changed content:

* Text inserted/deleted strictly before the tracked range shifts it; text
  changed after it leaves it alone.  Such outside-only changes are preserved.
* An edit whose span overlaps ``[start, end)``, or an insertion exactly at
  ``start`` or ``end``, conflicts.  For a zero-length range (an insertion
  point left behind by a deletion) an insertion at that exact point conflicts.
* Conflict detection scans every intermediate revision, so touching the range
  and later reverting the characters still blocks the decision.

The helpers are pure; the HTTP layer supplies recorded exact edits.
"""

from __future__ import annotations


def map_through_edit(
    start: int,
    end: int,
    edit_start: int,
    edit_end: int,
    inserted_length: int,
) -> tuple[int, int] | None:
    """Carry ``[start, end)`` across one exact replacement edit.

    The edit replaces ``[edit_start, edit_end)`` of the old content with
    ``inserted_length`` characters.  Returns ``None`` when the edit touches the
    range or one of its boundary positions.
    """
    if edit_start == edit_end:
        # Zero-width edit (pure insertion).  Inserting at ``start`` or ``end``
        # changes what an application at that boundary means.
        if start <= edit_start <= end:
            return None
        if end < edit_start:
            return start, end
        # start > edit_start: the whole range is pushed right.
        return start + inserted_length, end + inserted_length

    # Non-empty edit: touching any character of [start, end) is a conflict.
    if edit_start < end and edit_end > start:
        return None
    if end <= edit_start:
        return start, end
    # start >= edit_end: the whole range lies after the edited span.
    delta = inserted_length - (edit_end - edit_start)
    return start + delta, end + delta


def apply_exact_edit(
    content: str, edit_start: int, edit_end: int, replacement: str
) -> str:
    """Splice ``replacement`` over ``[edit_start, edit_end)``."""
    return content[:edit_start] + replacement + content[edit_end:]
