import asyncio
import json
from pathlib import Path

import fitz
import pytest

from app.config import Settings
from app.models import SummaryResult
from app.openrouter_client import (
    ManualReviewRequired,
    _merge_parser_and_model,
    _safe_openrouter_error_body,
    summarize_with_model,
)
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


def _clean_supported_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    rows = [
        "UG-06 - 13",
        "PC-01 - 2",
        "Project note: readable as-built text layer with supported billing totals.",
        "Planner note: field quantities are shown directly as supported code totals.",
        "Field note: no unresolved construction callouts are present for review.",
        "Verification note: deterministic parser evidence is sufficient for output.",
        "Readable note: project area has normal text extraction quality.",
        "Readable note: billing labels are visible and complete.",
        "Readable note: summary placement can use parser totals.",
        "Readable note: no material interpretation is requested.",
        "Readable note: no construction composite conversion is attempted.",
        "Readable note: deterministic evidence is preferred.",
        "Readable note: parser-only output is acceptable for this clean fixture.",
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
        content = self._payload if isinstance(self._payload, str) else json.dumps(self._payload)
        return {
            "choices": [
                {
                    "message": {
                        "content": content,
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


def test_model_quantity_disagreement_is_reported_without_replacing_parser_total() -> None:
    settings = Settings(OPENROUTER_API_KEY="test-key")
    model_summary = SummaryResult(
        model=settings.openrouter_model,
        job_totals=["UG-56 - 200'", "PC-01 - 1"],
        materials=[],
        warnings=[],
        confidence=0.88,
    )

    merged = _merge_parser_and_model(["UG-56 - 170'"], model_summary, settings)

    assert merged.job_totals == ["UG-56 - 170'"]
    assert "Possible extra totals were not added because the parsed PDF text did not support them." in merged.warnings
    assert any("different quantity" in warning.lower() for warning in merged.warnings)


def test_openrouter_error_body_redacts_key_urls_and_token_shapes() -> None:
    body = (
        "visit https://openrouter.ai/workspaces/default/keys/abc123secret "
        "Authorization: Bearer headersecret token sk-or-v1-secretvalue"
    )

    sanitized = _safe_openrouter_error_body(body)

    assert "abc123secret" not in sanitized
    assert "secretvalue" not in sanitized
    assert "headersecret" not in sanitized
    assert "https://openrouter.ai/workspaces/[redacted]/keys/[redacted]" in sanitized
    assert "sk-or-v1-[redacted]" in sanitized
    assert "Bearer [redacted]" in sanitized


def test_clean_supported_pdf_can_use_parser_without_openrouter() -> None:
    settings = Settings(
        OPENROUTER_API_KEY="",
        INCLUDE_PAGE_IMAGES=False,
    )

    summary = asyncio.run(summarize_with_model(_clean_supported_pdf(), settings))

    assert summary.model == "parser"
    assert summary.job_totals == ["UG-06 - 13", "PC-01 - 2"]
    assert summary.materials == []
    assert summary.warnings == []


def test_clean_supported_pdf_skips_openrouter_even_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.status_code = 500
    _FakeAsyncClient.payload = {}
    monkeypatch.setattr("app.openrouter_client.httpx.AsyncClient", _FakeAsyncClient)
    settings = Settings(
        OPENROUTER_API_KEY="test-key",
        INCLUDE_PAGE_IMAGES=False,
    )

    summary = asyncio.run(summarize_with_model(_clean_supported_pdf(), settings))

    assert _FakeAsyncClient.calls == []
    assert summary.model == "parser"
    assert summary.job_totals == ["UG-06 - 13", "PC-01 - 2"]
    assert summary.materials == []
    assert summary.warnings == []


def test_clean_supported_pdf_does_not_call_openrouter_when_parser_is_sufficient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.status_code = 200
    _FakeAsyncClient.payload = {
        "title": "MKR Job Totals",
        "job_totals": ["UG-06 - 13", "PC-01 - 2"],
        "materials": [],
        "warnings": [],
        "confidence": 0.9,
        "remaining_unresolved_callouts": [],
        "resolved_callouts": [],
    }
    monkeypatch.setattr("app.openrouter_client.httpx.AsyncClient", _FakeAsyncClient)
    settings = Settings(
        OPENROUTER_API_KEY="test-key",
        INCLUDE_PAGE_IMAGES=False,
    )

    summary = asyncio.run(summarize_with_model(_clean_supported_pdf(), settings))

    assert _FakeAsyncClient.calls == []
    assert summary.model == "parser"
    assert summary.job_totals == ["UG-06 - 13", "PC-01 - 2"]
    assert summary.materials == []
    assert summary.warnings == []


def test_clean_supported_pdf_uses_openrouter_when_materials_are_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.status_code = 200
    _FakeAsyncClient.payload = {
        "title": "MKR Job Totals",
        "job_totals": ["UG-06 - 13", "PC-01 - 2"],
        "materials": ["Fiber marker - 2"],
        "warnings": [],
        "confidence": 0.9,
        "remaining_unresolved_callouts": [],
        "resolved_callouts": [],
    }
    monkeypatch.setattr("app.openrouter_client.httpx.AsyncClient", _FakeAsyncClient)
    settings = Settings(
        OPENROUTER_API_KEY="test-key",
        INCLUDE_MATERIALS=True,
        INCLUDE_PAGE_IMAGES=False,
    )

    summary = asyncio.run(summarize_with_model(_clean_supported_pdf(), settings))

    assert _FakeAsyncClient.calls
    assert summary.model == f"parser+{settings.openrouter_model}"
    assert summary.job_totals == ["UG-06 - 13", "PC-01 - 2"]
    assert summary.materials == ["Fiber marker - 2"]
    assert summary.warnings == []


def test_known_sample_requires_manual_review_without_page_image_verification(monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert exc.value.verifier_model == settings.openrouter_model
    assert exc.value.verifier_used is True


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
    assert exc.value.verifier_model == settings.openrouter_model
    assert exc.value.verifier_used is True
    assert "Model verifier kept EOL callout in manual review." in exc.value.warnings
    assert (
        "OpenRouter verifier reviewed unresolved callouts but could not clear them from parsed evidence."
        in exc.value.warnings
    )


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
    assert exc.value.verifier_model == settings.openrouter_model
    assert exc.value.verifier_used is True
    assert "Model omitted the unresolved callout." in exc.value.warnings


def test_openrouter_cannot_clear_unresolved_callout_with_ungrounded_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.status_code = 200
    _FakeAsyncClient.payload = {
        "title": "MKR Job Totals",
        "job_totals": ["UG-06 - 13"],
        "materials": [],
        "warnings": ["Model claimed the EOL callout was covered."],
        "confidence": 0.88,
        "remaining_unresolved_callouts": [],
        "resolved_callouts": [
            {
                "callout": "EOL - 48Ct - 66'",
                "resolution": "No additional MKR total is needed.",
                "evidence": "Supervisor confirmed this outside the parsed drawing.",
            }
        ],
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
    assert exc.value.verifier_model == settings.openrouter_model
    assert exc.value.verifier_used is True
    assert "Model claimed the EOL callout was covered." in exc.value.warnings
    assert any("not found in parsed PDF evidence" in warning for warning in exc.value.warnings)


def test_openrouter_cannot_clear_unresolved_callout_with_only_parser_total_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.status_code = 200
    _FakeAsyncClient.payload = {
        "title": "MKR Job Totals",
        "job_totals": ["UG-06 - 13"],
        "materials": [],
        "warnings": ["Model claimed a supported total covered the EOL callout."],
        "confidence": 0.88,
        "remaining_unresolved_callouts": [],
        "resolved_callouts": [
            {
                "callout": "EOL - 48Ct - 66'",
                "resolution": "UG-06 covers the EOL work.",
                "evidence": "UG-06 - 13",
            }
        ],
    }
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
    assert any("not found in parsed PDF evidence" in warning for warning in exc.value.warnings)


def test_openrouter_cannot_clear_unresolved_callout_with_unrelated_parsed_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.status_code = 200
    _FakeAsyncClient.payload = {
        "title": "MKR Job Totals",
        "job_totals": ["UG-06 - 13"],
        "materials": [],
        "warnings": ["Model cited an unrelated parsed note."],
        "confidence": 0.88,
        "remaining_unresolved_callouts": [],
        "resolved_callouts": [
            {
                "callout": "EOL - 48Ct - 66'",
                "resolution": "No additional MKR total is needed.",
                "evidence": "Planner note: work area has enough visible text for automatic parser review.",
            }
        ],
    }
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
    assert any("not found in parsed PDF evidence" in warning for warning in exc.value.warnings)


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
    assert exc.value.verifier_model == settings.openrouter_model
    assert exc.value.verifier_used is False
    assert "OpenRouter verifier was unavailable (OpenRouter returned 401); manual review is required." in exc.value.warnings


def test_malformed_openrouter_response_falls_back_to_manual_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.status_code = 200
    _FakeAsyncClient.payload = "not json"
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
    assert exc.value.verifier_model == settings.openrouter_model
    assert exc.value.verifier_used is False
    assert "OpenRouter verifier was unavailable (Model did not return JSON.); manual review is required." in exc.value.warnings


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
