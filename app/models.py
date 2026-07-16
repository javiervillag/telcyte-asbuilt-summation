from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class SummaryIssue(BaseModel):
    severity: Literal["blocker", "action", "notice"]
    code: str
    message: str
    subject: Optional[str] = None


class EvidencePart(BaseModel):
    page: int
    bbox: tuple[float, float, float, float]
    line: str
    qty: str


class BillingEvidence(BaseModel):
    key: str
    display: str
    total: str
    parts: list[EvidencePart] = Field(default_factory=list)


class DeltaEvidence(BaseModel):
    key: str
    display: str
    cumulative: str
    previously_billed: str
    new: str


class MaterialEvidence(BaseModel):
    part: str
    display: str
    rule: str
    source_quantity: Optional[str] = None
    source_lines: list[str] = Field(default_factory=list)
    result: str


class SummaryEvidence(BaseModel):
    billing: list[BillingEvidence] = Field(default_factory=list)
    delta: list[DeltaEvidence] = Field(default_factory=list)
    materials: list[MaterialEvidence] = Field(default_factory=list)
    preview_pages: list[int] = Field(default_factory=list)


class CableFootageItem(BaseModel):
    label: str = ""
    page: int = 0
    feet: float = 0.0
    source: str = ""


class CableFootageLine(BaseModel):
    callout: str
    display_type: str
    part_number: str = ""
    family: str
    path_segments: list[CableFootageItem] = Field(default_factory=list)
    storage_items: list[CableFootageItem] = Field(default_factory=list)
    path_subtotal: float = 0.0
    storage_subtotal: float = 0.0
    # "comp15" is retained for run-history payloads written before multi-code
    # path support (2026-07); new results emit "path_codes"/"tail_sequence".
    path_source: Literal[
        "comp15",
        "path_codes",
        "fallback_codes",
        "station_markers",
        "tail_sequence",
        "unassigned",
    ] = "unassigned"
    included_storage_ft: float = 0.0
    subtotal_used: float = 0.0
    buffered_ft_before_rounding: Optional[float] = None
    buffer: float = 1.1
    rounding: str = "ceil_100"
    total_ft: Optional[int] = None
    material_line: str = ""
    review_material_line: str = ""
    eligible_for_stamp: bool = False
    source_pages: list[int] = Field(default_factory=list)
    confidence: float = 0.0
    review_flags: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class SummaryResult(BaseModel):
    title: str = "MKR Job Totals"
    job_totals: list[str] = Field(default_factory=list)
    extra_totals: list[str] = Field(default_factory=list)
    extra_notes: list[str] = Field(default_factory=list)
    materials: list[str] = Field(default_factory=list)
    new_totals: list[str] = Field(default_factory=list)
    cable_footage: list[CableFootageLine] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    informational_notes: list[str] = Field(default_factory=list)
    issues: list[SummaryIssue] = Field(default_factory=list)
    evidence: SummaryEvidence = Field(default_factory=SummaryEvidence, exclude=True)
    # Populated by the annotator from the actual Materials-box merge. It is
    # runtime classification evidence, not part of the public response model.
    final_material_rows: list[str] = Field(default_factory=list, exclude=True)
    confidence: float = 0.0
    model: str
    # Per-page billing-code totals for multi-page as-builts (the "MKR Page Totals"
    # boxes). Keyed by 1-based page number; billing codes only - no materials/extras.
    page_totals: dict[int, list[str]] = Field(default_factory=dict)

    def with_eligible_cable_materials(self) -> "SummaryResult":
        materials = list(self.materials)
        changed = False
        for line in self.cable_footage:
            if line.eligible_for_stamp and line.material_line and line.material_line not in materials:
                materials.append(line.material_line)
                changed = True
            if line.review_material_line and line.review_material_line not in materials:
                materials.append(line.review_material_line)
                changed = True
        if not changed:
            return self
        return self.model_copy(update={"materials": materials})

    def totals_box_lines(self) -> list[str]:
        lines = [self.title.strip() or "MKR Job Totals"]
        lines.extend(line.strip() for line in self.job_totals if line.strip())
        if self.extra_totals:
            lines.append("User-selected extra totals")
            lines.extend(line.strip() for line in self.extra_totals if line.strip())
        if self.extra_notes:
            lines.append("Extra notes")
            lines.extend(line.strip() for line in self.extra_notes if line.strip())
        return lines

    def material_box_lines(self) -> list[str]:
        if not self.materials:
            return []
        return ["Materials", *[line.strip() for line in self.materials if line.strip()]]

    def new_totals_box_lines(self) -> list[str]:
        rows = [line.strip() for line in self.new_totals if line.strip()]
        if not rows:
            return []
        return ["MKR New Totals", "Additions", *rows]

    def page_totals_box_lines(self, page: int) -> list[str]:
        # Page Totals box for a single page: billing codes only, titled distinctly
        # from the page-1 Job Totals box. Empty when the page carries no codes.
        rows = [line.strip() for line in self.page_totals.get(page, []) if line.strip()]
        if not rows:
            return []
        return ["MKR Page Totals", *rows]

    def display_lines(self) -> list[str]:
        lines = self.totals_box_lines()
        new_totals = self.new_totals_box_lines()
        if new_totals:
            lines.extend(new_totals)
        material_heading = "Materials" if len(self.materials) != 1 else "Material"
        if self.materials:
            lines.append(material_heading)
            lines.extend(line.strip() for line in self.materials if line.strip())
        # Warnings are intentionally NOT stamped into the PDF box; they remain
        # available in the API response and run history (Nick Evans, 2026-06-09
        # sync: remove the Review section from the totals box).
        return lines
