"""Shared pytest fixtures for AlphaLens test suite."""

from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture()
def html_10k() -> bytes:
    return (FIXTURES_DIR / "sample_10k.html").read_bytes()


@pytest.fixture()
def html_10q() -> bytes:
    return (FIXTURES_DIR / "sample_10q.html").read_bytes()


@pytest.fixture()
def html_minimal() -> bytes:
    return (FIXTURES_DIR / "sample_minimal.html").read_bytes()
