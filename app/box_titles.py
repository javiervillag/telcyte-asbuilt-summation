"""Single source of truth for recognizing stamped output-box titles.

Both the parser (re-run exclusion of previously stamped boxes) and the annotator
(find/replace existing boxes) must agree on what a stamped Job Totals, Page Totals,
or Materials box looks like. Keeping the literals and predicates here prevents the
two sides from drifting and silently re-opening the re-run double-count or breaking
box replacement (Nick Evans, June-23 sync: NR-996825 page-totals boxes double-counted).
"""
from __future__ import annotations

import re


def normalized_title(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).lower()


def starts_with_job_totals_title(text: str) -> bool:
    return normalized_title(text).startswith("mkr job totals")


def starts_with_page_totals_title(text: str) -> bool:
    return normalized_title(text).startswith("mkr page totals")


def starts_with_new_totals_title(text: str) -> bool:
    """A manual "MKR New Totals" box - Nick's yellow added-scope summary on a partial
    as-built (original work in green "MKR Job/Page Totals - Previously Billed" boxes,
    later additions in a yellow "MKR New Totals - Add resto" box). Like the other totals
    boxes it is a SUMMARY, never field evidence, so it must be excluded from counting -
    but it is the customer's own annotation, so the annotator must NOT replace it. That
    split is why only the combined predicate below includes it (Nick, NR-996825 PRJ10)."""
    return normalized_title(text).startswith("mkr new totals")


def starts_with_totals_title(text: str) -> bool:
    """Any MKR totals SUMMARY box - Job, Page, or New - all of which must be excluded
    from field-evidence counting so their lines are never summed as callouts. The
    annotator's replace paths use the specific job/page predicates only, so a "New
    Totals" box is excluded from counting yet preserved untouched on the page."""
    return (
        starts_with_job_totals_title(text)
        or starts_with_page_totals_title(text)
        or starts_with_new_totals_title(text)
    )


def starts_with_materials_title(text: str) -> bool:
    for line in text.splitlines():
        if line.strip():
            return line.strip().lower() in {"material", "materials"}
    return False
