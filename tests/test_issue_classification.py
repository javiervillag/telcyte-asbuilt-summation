from app.config import Settings
from app.models import SummaryResult
from app.openrouter_client import _merge_parser_and_model, _normalize_summary


def test_known_model_only_billing_code_is_action() -> None:
    result = _merge_parser_and_model(
        ["UG-56 - 100"],
        SummaryResult(model="fake", job_totals=["UG-56 - 100", "UG-54 - 20"]),
        Settings(ALLOW_LLM_INFERRED_TOTALS=False),
    )

    issue = next(issue for issue in result.issues if issue.code == "model_omitted_known_code")
    assert issue.severity == "action"


def test_non_code_model_extra_is_notice() -> None:
    result = _merge_parser_and_model(
        ["UG-56 - 100"],
        SummaryResult(model="fake", job_totals=["UG-56 - 100", "Storage - 20"]),
        Settings(ALLOW_LLM_INFERRED_TOTALS=False),
    )

    issue = next(issue for issue in result.issues if issue.code == "model_extras_not_added")
    assert issue.severity == "notice"


def test_free_form_model_warning_is_notice_until_structured_comparison_promotes_it() -> None:
    result = _normalize_summary(
        {
            "job_totals": ["UG-56 - 100"],
            "warnings": ["Confirm the square-foot unit shown on the drawing."],
            "confidence": 0.9,
        },
        "fake",
    )

    assert result.issues[0].severity == "notice"
    assert result.issues[0].code == "model_review_note"
