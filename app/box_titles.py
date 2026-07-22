"""Single source of truth for recognizing stamped output-box titles.

Both the parser (re-run exclusion of previously stamped boxes) and the annotator
(find/replace existing boxes) must agree on what a stamped Job Totals, Page Totals,
or Materials box looks like. Keeping the literals and predicates here prevents the
two sides from drifting and silently re-opening the re-run double-count or breaking
box replacement (Nick Evans, June-23 sync: NR-996825 page-totals boxes double-counted).
"""
from __future__ import annotations

import re
from enum import Enum


_CANONICAL_JOB_TOTAL_RE = re.compile(r"^mkr\s+job\s+totals\b", re.I)
_LEGACY_JOB_TOTAL_RE = re.compile(r"^mkr\s+job\s+totals?\b", re.I)
_CANONICAL_PAGE_TOTAL_RE = re.compile(r"^mkr\s+page\s+totals\b", re.I)
_LEGACY_NUMBERED_PAGE_TOTAL_RE = re.compile(
    r"^(?:pg|page)\s+\d+\s+totals?\b",
    re.I,
)
_CANONICAL_NEW_TOTAL_RE = re.compile(r"^mkr\s+new\s+totals\b", re.I)


class TotalsTitleKind(str, Enum):
    JOB = "job"
    PAGE = "page"
    NEW = "new"


def normalized_title(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).lower()


def normalized_heading(text: str) -> str:
    """Normalize enough leading lines to recognize split PDF box titles."""
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    return normalized_title(" ".join(lines[:2]))


def totals_title_kind(text: str) -> TotalsTitleKind | None:
    heading = normalized_heading(text)
    if _CANONICAL_JOB_TOTAL_RE.match(heading) or _LEGACY_JOB_TOTAL_RE.match(heading):
        return TotalsTitleKind.JOB
    if _CANONICAL_PAGE_TOTAL_RE.match(heading) or _LEGACY_NUMBERED_PAGE_TOTAL_RE.match(heading):
        return TotalsTitleKind.PAGE
    if _CANONICAL_NEW_TOTAL_RE.match(heading):
        return TotalsTitleKind.NEW
    return None


def is_legacy_totals_alias(text: str) -> bool:
    heading = normalized_heading(text)
    legacy_job = bool(_LEGACY_JOB_TOTAL_RE.match(heading)) and not bool(
        _CANONICAL_JOB_TOTAL_RE.match(heading)
    )
    legacy_page = bool(_LEGACY_NUMBERED_PAGE_TOTAL_RE.match(heading))
    return legacy_job or legacy_page


def starts_with_job_totals_title(text: str) -> bool:
    return totals_title_kind(text) == TotalsTitleKind.JOB


def starts_with_page_totals_title(text: str) -> bool:
    return totals_title_kind(text) == TotalsTitleKind.PAGE


def starts_with_new_totals_title(text: str) -> bool:
    """A manual "MKR New Totals" box - Nick's yellow added-scope summary on a partial
    as-built (original work in green "MKR Job/Page Totals - Previously Billed" boxes,
    later additions in a yellow "MKR New Totals - Add resto" box). Like the other totals
    boxes it is a SUMMARY, never field evidence, so it must be excluded from counting -
    but it is the customer's own annotation, so the annotator must NOT replace it. That
    split is why only the combined predicate below includes it (Nick, NR-996825 PRJ10)."""
    return totals_title_kind(text) == TotalsTitleKind.NEW


def is_tool_new_totals_box(text: str) -> bool:
    lines = [line.strip().lower() for line in str(text or "").splitlines() if line.strip()]
    return len(lines) >= 2 and lines[0] == "mkr new totals" and lines[1] == "additions"


def is_previously_billed_totals_box(text: str) -> bool:
    return starts_with_totals_title(text) and "previously billed" in normalized_title(text)


def starts_with_totals_title(text: str) -> bool:
    """Any MKR totals SUMMARY box - Job, Page, or New - all of which must be excluded
    from field-evidence counting so their lines are never summed as callouts. The
    annotator's replace paths use the specific job/page predicates only, so a "New
    Totals" box is excluded from counting yet preserved untouched on the page."""
    return totals_title_kind(text) is not None


def starts_with_materials_title(text: str) -> bool:
    for line in text.splitlines():
        if line.strip():
            return line.strip().lower() in {"material", "materials"}
    return False
