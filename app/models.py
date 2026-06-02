from __future__ import annotations

from pydantic import BaseModel, Field


class SummaryResult(BaseModel):
    title: str = "MKR Job Totals"
    job_totals: list[str] = Field(default_factory=list)
    materials: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    model: str

    def display_lines(self) -> list[str]:
        lines = [self.title.strip() or "MKR Job Totals"]
        lines.extend(line.strip() for line in self.job_totals if line.strip())
        material_heading = "Materials" if len(self.materials) != 1 else "Material"
        if self.materials:
            lines.append(material_heading)
            lines.extend(line.strip() for line in self.materials if line.strip())
        if self.warnings:
            lines.append("Review")
            lines.extend(line.strip() for line in self.warnings if line.strip())
        return lines
