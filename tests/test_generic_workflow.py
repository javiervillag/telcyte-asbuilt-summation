import asyncio
from pathlib import Path

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


def test_known_sample_requires_manual_review_without_page_image_verification() -> None:
    settings = Settings(
        OPENROUTER_API_KEY="not-used",
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


def test_known_sample_requires_manual_review_even_with_page_images() -> None:
    settings = Settings(
        OPENROUTER_API_KEY="not-used",
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
