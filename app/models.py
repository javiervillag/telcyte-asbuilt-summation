from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


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
    buffer: float = 1.1
    rounding: str = "ceil_100"
    total_ft: Optional[int] = None
    material_line: str = ""
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
    cable_footage: list[CableFootageLine] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    informational_notes: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    model: str

    def with_eligible_cable_materials(self) -> "SummaryResult":
        materials = list(self.materials)
        changed = False
        for line in self.cable_footage:
            if line.eligible_for_stamp and line.material_line and line.material_line not in materials:
                materials.append(line.material_line)
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

    def display_lines(self) -> list[str]:
        lines = self.totals_box_lines()
        material_heading = "Materials" if len(self.materials) != 1 else "Material"
        if self.materials:
            lines.append(material_heading)
            lines.extend(line.strip() for line in self.materials if line.strip())
        # Warnings are intentionally NOT stamped into the PDF box; they remain
        # available in the API response and run history (Nick Evans, 2026-06-09
        # sync: remove the Review section from the totals box).
        return lines
