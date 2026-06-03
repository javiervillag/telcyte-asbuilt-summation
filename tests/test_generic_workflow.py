import asyncio
import json
from pathlib import Path

import fitz
import pytest

from app.config import Settings
from app.openrouter_client import ManualReviewRequired, summarize_with_model
from app.pdf_parser import diagnose_extraction, derive_code_totals, extract_text_blocks
from app.rate_cards import total_line_key
from tests.fixtures.expected_samples import summary_for_source


SAMPLES = Path("/Users/javiervillaguardado/Downloads/Asbuilt Examples for AI Summation")
RL_SAMPLE = SAMPLES / "COAX-ASBUILT-(TelCyte)-RL-248790-Totals Removed.pdf"
SAMPLE_NAMES = [
    "COAX-ASBUILT-(TelCyte)-RL-248790-Totals Removed.pdf",
    "FIBER-ASBUILT-(TelCyte)-BI-596045-Totals Removed.pdf",
    "FIBER-ASBUILT-(TelCyte)-BI-829050-Totals Removed.pdf",
    "FIBER-ASBUILT-(TelCyte)-BI-864045-Totals Removed.pdf",
    "FIBER-ASBUILT-(TelCyte)-BI-912047-Totals Removed.pdf",
]


def _reviewable_unresolved_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    rows = [
        "UG-06 - 13",
        "EOL - 48Ct - 66'",
        "Project note: readable as-built text layer with construction quantity context.",
        "Planner note: work area has enough visible text for automatic parser review.",
        "Field note: quantities are shown in annotation-style text blocks.",
        "Verification note: this drawing includes one unresolved construction callout.",
    ]
    for index, row in enumerate(rows):
        page.insert_text((72, 72 + index * 26), row)
    content = doc.tobytes()
    doc.close()
    return content


class _FakeOpenRouterResponse:
    def __init__(self, payload: dict, status_code: int = 200, text: str = "ok"):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self) -> dict:
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(self._payload),
                    }
                }
            ]
        }


class _FakeAsyncClient:
    calls: list[dict] = []
    payload: dict = {}
    status_code: int = 200
    response_text: str = "ok"

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, headers, json):
        self.calls.append({"url": url, "headers": headers, "payload": json})
        return _FakeOpenRouterResponse(self.payload, self.status_code, self.response_text)


def test_sample_id_does_not_return_hardcoded_summary_without_evidence() -> None:
    blocks = extract_text_blocks(RL_SAMPLE.read_bytes())
    totals = derive_code_totals(blocks)
    diagnostics = diagnose_extraction(blocks, totals)

    assert totals
    assert diagnostics.review_required is True
    assert diagnostics.unresolved_callout_count > 0
    assert "Readable construction callouts require rate-card/composite interpretation" in " ".join(diagnostics.warnings)
    assert any("EOL" in callout for callout in diagnostics.unresolved_callouts)


@pytest.mark.parametrize("sample_name", SAMPLE_NAMES)
def test_samples_are_regression_inputs_not_filename_answers(sample_name: str) -> None:
    expected = summary_for_source(sample_name)
    assert expected is not None

    blocks = extract_text_blocks((SAMPLES / sample_name).read_bytes())
    totals = derive_code_totals(blocks)
    diagnostics = diagnose_extraction(blocks, totals)

    expected_keys = {total_line_key(line) for line in expected.job_totals}
    total_keys = {total_line_key(line) for line in totals}
    missing_expected_totals = expected_keys - total_keys
    assert missing_expected_totals
    assert diagnostics.review_required is True
    assert diagnostics.unresolved_callout_count or diagnostics.ambiguous_code_line_count


def test_known_sample_requires_manual_review_without_page_image_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.payload = {
        "title": "MKR Job Totals",
        "job_totals": [],
        "materials": [],
        "warnings": ["Model verifier could not resolve the construction callouts."],
        "confidence": 0.5,
        "remaining_unresolved_callouts": ["EOL - .625", "Tie Point - .625"],
        "resolved_callouts": [],
    }
    monkeypatch.setattr("app.openrouter_client.httpx.AsyncClient", _FakeAsyncClient)
    settings = Settings(
        OPENROUTER_API_KEY="test-key",
        INCLUDE_PAGE_IMAGES=False,
    )

    with pytest.raises(ManualReviewRequired) as exc:
        asyncio.run(
            summarize_with_model(
                RL_SAMPLE.read_bytes(),
                settings,
                source_name=RL_SAMPLE.name,
            )
        )

    assert "Readable construction callouts require rate-card/composite interpretation" in " ".join(exc.value.warnings)
    assert _FakeAsyncClient.calls


def test_unresolved_callouts_are_sent_to_openrouter_before_manual_review(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.status_code = 200
    _FakeAsyncClient.payload = {
        "title": "MKR Job Totals",
        "job_totals": ["UG-06 - 13"],
        "materials": [],
        "warnings": ["Model verifier kept EOL callout in manual review."],
        "confidence": 0.72,
        "remaining_unresolved_callouts": ["EOL - 48Ct - 66'"],
        "resolved_callouts": [],
    }
    monkeypatch.setattr("app.openrouter_client.httpx.AsyncClient", _FakeAsyncClient)
    settings = Settings(
        OPENROUTER_API_KEY="test-key",
        INCLUDE_PAGE_IMAGES=False,
    )

    with pytest.raises(ManualReviewRequired) as exc:
        asyncio.run(summarize_with_model(_reviewable_unresolved_pdf(), settings))

    assert _FakeAsyncClient.calls
    prompt_text = _FakeAsyncClient.calls[0]["payload"]["messages"][1]["content"][0]["text"]
    assert "Unresolved construction callouts needing verifier review" in prompt_text
    assert "EOL - 48Ct - 66'" in prompt_text
    assert exc.value.supported_totals == ["UG-06 - 13"]
    assert exc.value.unresolved_callouts == ["EOL - 48Ct - 66'"]
    assert "Model verifier kept EOL callout in manual review." in exc.value.warnings


def test_openrouter_cannot_clear_unresolved_callout_without_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.status_code = 200
    _FakeAsyncClient.payload = {
        "title": "MKR Job Totals",
        "job_totals": ["UG-06 - 13"],
        "materials": [],
        "warnings": ["Model omitted the unresolved callout."],
        "confidence": 0.88,
        "remaining_unresolved_callouts": [],
        "resolved_callouts": [],
    }
    monkeypatch.setattr("app.openrouter_client.httpx.AsyncClient", _FakeAsyncClient)
    settings = Settings(
        OPENROUTER_API_KEY="test-key",
        INCLUDE_PAGE_IMAGES=False,
    )

    with pytest.raises(ManualReviewRequired) as exc:
        asyncio.run(summarize_with_model(_reviewable_unresolved_pdf(), settings))

    assert _FakeAsyncClient.calls
    assert exc.value.unresolved_callouts == ["EOL - 48Ct - 66'"]
    assert "Model omitted the unresolved callout." in exc.value.warnings


def test_openrouter_error_falls_back_to_manual_review_for_unresolved_callouts(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.status_code = 401
    _FakeAsyncClient.response_text = '{"error":"bad key"}'
    _FakeAsyncClient.payload = {}
    monkeypatch.setattr("app.openrouter_client.httpx.AsyncClient", _FakeAsyncClient)
    settings = Settings(
        OPENROUTER_API_KEY="test-key",
        INCLUDE_PAGE_IMAGES=False,
    )

    with pytest.raises(ManualReviewRequired) as exc:
        asyncio.run(summarize_with_model(_reviewable_unresolved_pdf(), settings))

    assert _FakeAsyncClient.calls
    assert exc.value.supported_totals == ["UG-06 - 13"]
    assert exc.value.unresolved_callouts == ["EOL - 48Ct - 66'"]
    assert "OpenRouter verifier was unavailable (OpenRouter returned 401); manual review is required." in exc.value.warnings


def test_known_sample_requires_manual_review_even_with_page_images(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.status_code = 200
    _FakeAsyncClient.payload = {
        "title": "MKR Job Totals",
        "job_totals": [],
        "materials": [],
        "warnings": ["Model verifier could not resolve the construction callouts."],
        "confidence": 0.5,
        "remaining_unresolved_callouts": ["EOL - .625", "Tie Point - .625"],
        "resolved_callouts": [],
    }
    monkeypatch.setattr("app.openrouter_client.httpx.AsyncClient", _FakeAsyncClient)
    settings = Settings(
        OPENROUTER_API_KEY="test-key",
        INCLUDE_PAGE_IMAGES=True,
    )

    with pytest.raises(ManualReviewRequired) as exc:
        asyncio.run(
            summarize_with_model(
                RL_SAMPLE.read_bytes(),
                settings,
                source_name=RL_SAMPLE.name,
            )
        )

    assert "Readable construction callouts require rate-card/composite interpretation" in " ".join(exc.value.warnings)
    assert _FakeAsyncClient.calls
