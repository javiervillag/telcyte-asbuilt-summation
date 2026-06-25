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


def starts_with_totals_title(text: str) -> bool:
    """Either a Job Totals or a Page Totals box. Used where BOTH must be treated as
    prior output (e.g. the parser's re-run exclusion). The annotator's job/page
    replace paths use the specific predicates so the paths can never cross."""
    return starts_with_job_totals_title(text) or starts_with_page_totals_title(text)


def starts_with_materials_title(text: str) -> bool:
    for line in text.splitlines():
        if line.strip():
            return line.strip().lower() in {"material", "materials"}
    return False
