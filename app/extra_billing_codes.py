from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

from pydantic import BaseModel, Field

from app.rate_cards import code_key


class ExtraBillingCode(BaseModel):
    code: str
    category: str
    name: str
    unit: str
    description: str
    when_to_consider: str


class ExtraBillingCodeSelection(BaseModel):
    code: str
    quantity: str = Field(min_length=1, max_length=24)
    note: str = Field(default="", max_length=180)


EXTRA_BILLING_CODES: tuple[ExtraBillingCode, ...] = (
    ExtraBillingCode(
        code="PC-01",
        category="Preconstruction",
        name="Private locates",
        unit="each",
        description="Locating private utilities needed to complete the project.",
        when_to_consider="Use when private utility locating is required and approved.",
    ),
    ExtraBillingCode(
        code="PC-02",
        category="Preconstruction",
        name="White lining / pre-locates",
        unit="each",
        description="Marking proposed excavation or demolition limits before utility ticket submission.",
        when_to_consider="Use when the as-built or work order includes white-lining or pre-locate work.",
    ),
    ExtraBillingCode(
        code="CX-06",
        category="Coax/HFC",
        name="Splice/remove passive",
        unit="each",
        description="Install or remove a passive straight/tap splice or related active/passive device.",
        when_to_consider="Use for confirmed coax passive, straight-splice, tap, or device work.",
    ),
    ExtraBillingCode(
        code="CX-09",
        category="Coax/HFC",
        name="Splicing passive splitter",
        unit="each",
        description="Install a passive splitter, including cable forming, straps, spacers, and shrink boot.",
        when_to_consider="Use when splitter/passive splicing is known from the drawing or work context.",
    ),
    ExtraBillingCode(
        code="FB-01",
        category="Fiber",
        name="Existing enclosure / re-entry",
        unit="each",
        description="Access and prep an existing enclosure, cable, trays, sealing, and re-installation.",
        when_to_consider="Use when the work includes re-entry into an existing fiber enclosure.",
    ),
    ExtraBillingCode(
        code="FB-04",
        category="Fiber",
        name="Fusion splice optical fiber",
        unit="each",
        description="Fusion splice work for 1-144 fibers, including prep, trays, heat shrink, sealing, and documentation.",
        when_to_consider="Use when fiber splice groups are confirmed, such as 48-count EOL/tie/storage work.",
    ),
    ExtraBillingCode(
        code="FB-10",
        category="Fiber",
        name="Install patch panel",
        unit="each",
        description="Install wall or rack mounted termination equipment with grounding, securing, and labeling.",
        when_to_consider="Use when patch-panel or termination equipment installation is confirmed.",
    ),
    ExtraBillingCode(
        code="FB-15",
        category="Fiber",
        name="Power testing",
        unit="each",
        description="Optical power testing at 1310/1550 after fiber construction.",
        when_to_consider="Use when the fiber work requires documented optical power testing.",
    ),
    ExtraBillingCode(
        code="PT-02",
        category="Performance/testing",
        name="End of line tests",
        unit="each",
        description="EOL location checks, grounding verification, signal/sweep testing, modem test documentation, and troubleshooting.",
        when_to_consider="Use for confirmed coax/HFC end-of-line test work.",
    ),
    ExtraBillingCode(
        code="TL-05",
        category="Time/labor",
        name="Fiber technician troubleshooting",
        unit="hour",
        description="One fiber technician with vehicle, tools, and equipment for troubleshooting.",
        when_to_consider="Use only when fiber troubleshooting time is approved.",
    ),
    ExtraBillingCode(
        code="TL-06",
        category="Time/labor",
        name="HFC technician troubleshooting",
        unit="hour",
        description="One HFC technician with vehicle, tools, and equipment for troubleshooting.",
        when_to_consider="Use only when HFC troubleshooting time is approved.",
    ),
    ExtraBillingCode(
        code="TL-20",
        category="Time/labor",
        name="Setup charge",
        unit="each",
        description="Setup of contractor forces and equipment for aerial, underground, or splicing work.",
        when_to_consider="Use only with Cox authorization and the applicable CWO threshold context.",
    ),
    ExtraBillingCode(
        code="COMP-2",
        category="Composite",
        name="Demarcation",
        unit="each",
        description="Composite demarcation item.",
        when_to_consider="Use when demarcation work is confirmed by the project context.",
    ),
    ExtraBillingCode(
        code="COMP-13",
        category="Composite",
        name="Splice case management",
        unit="each",
        description="Composite splice-case management for new or existing optical splice case locations.",
        when_to_consider="Use when splice-case management is confirmed; quantity should come from Telcyte billing context.",
    ),
)

CATEGORY_ORDER = (
    "Preconstruction",
    "Coax/HFC",
    "Fiber",
    "Performance/testing",
    "Time/labor",
    "Composite",
)

_CATALOG_BY_KEY = {
    code_key(item.code): item
    for item in EXTRA_BILLING_CODES
    if code_key(item.code)
}


def grouped_extra_billing_codes() -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in EXTRA_BILLING_CODES:
        groups[item.category].append(item.model_dump())
    return [
        {"name": category, "codes": groups[category]}
        for category in CATEGORY_ORDER
        if groups.get(category)
    ]


def parse_extra_billing_code_selections(raw: str | None) -> list[ExtraBillingCodeSelection]:
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Extra billing codes must be valid JSON.") from exc
    if payload in (None, ""):
        return []
    if not isinstance(payload, list):
        raise ValueError("Extra billing codes must be a list.")

    selections: list[ExtraBillingCodeSelection] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("Each extra billing code must be an object.")
        code = _normalize_code(item.get("code"))
        catalog_item = catalog_item_for_code(code)
        if catalog_item:
            code = catalog_item.code
        elif not _is_manual_code(code):
            raise ValueError(f"{code or 'Unknown code'} is not a valid manual extra billing code.")
        quantity = _normalize_quantity(item.get("quantity"))
        note = re.sub(r"\s+", " ", str(item.get("note") or "")).strip()
        selections.append(ExtraBillingCodeSelection(code=code, quantity=quantity, note=note))
    return selections


def catalog_item_for_code(raw_code: str) -> ExtraBillingCode | None:
    key = code_key(raw_code)
    if not key:
        return None
    return _CATALOG_BY_KEY.get(key)


def extra_totals_from_selections(selections: list[ExtraBillingCodeSelection]) -> tuple[list[str], list[str]]:
    totals: list[str] = []
    notes: list[str] = []
    for selection in selections:
        totals.append(f"{selection.code} - {selection.quantity}")
        if selection.note:
            notes.append(f"{selection.code}: {selection.note}")
    return totals, notes


def _normalize_quantity(value: object) -> str:
    quantity = re.sub(r"\s+", " ", str(value or "")).strip()
    if not quantity:
        raise ValueError("Each selected extra code needs a quantity.")
    if not re.fullmatch(r"\d+(?:\.\d+)?(?:\s*(?:'|sqft|hr|hrs|ea|each))?", quantity, re.I):
        raise ValueError("Extra-code quantity must be a number, optionally followed by ', sqft, hr, or each.")
    return quantity


def _normalize_code(value: object) -> str:
    code = re.sub(r"\s+", "", str(value or "")).strip().upper()
    return re.sub(r"^([A-Z]{2,6})(\d)", r"\1-\2", code)


def _is_manual_code(code: str) -> bool:
    return bool(re.fullmatch(r"[A-Z]{2,6}-\d{1,4}[A-Z]?", code))
