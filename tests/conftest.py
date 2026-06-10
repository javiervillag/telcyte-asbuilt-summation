"""Shared test plumbing.

The integration tests reference local sample as-built PDFs that only exist on
the developer's machine. Anywhere else (CI, other machines) those tests SKIP
instead of failing, so `pytest` is green everywhere and new sample-based tests
need no special handling.
"""
from __future__ import annotations

import pytest

_SAMPLES_MARKER = "Asbuilt Examples for AI Summation"


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    try:
        return (yield)
    except FileNotFoundError as exc:
        if _SAMPLES_MARKER in str(exc):
            pytest.skip(f"local sample PDF not available: {exc}")
        raise
    except RuntimeError as exc:
        # PyMuPDF raises its own FileNotFoundError subclass of RuntimeError.
        if "no such file" in str(exc).lower() and _SAMPLES_MARKER in str(exc):
            pytest.skip(f"local sample PDF not available: {exc}")
        raise
