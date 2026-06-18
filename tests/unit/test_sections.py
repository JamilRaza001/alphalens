"""Unit tests for src/alphalens/etl/sections.py — S6 Section Detector.

All tests are offline (no network). Fixtures loaded from tests/fixtures/.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from pydantic import ValidationError

from alphalens.config import get_settings
from alphalens.etl.edgar import EdgarClient
from alphalens.etl.sections import Section, SectionDetector

# ---------------------------------------------------------------------------
# AC #11 — construction
# ---------------------------------------------------------------------------


def test_unsupported_form_type_raises_value_error() -> None:
    with pytest.raises(ValueError, match="10-X"):
        SectionDetector("10-X")  # type: ignore[arg-type]


def test_supported_form_types_do_not_raise() -> None:
    SectionDetector("10-K")
    SectionDetector("10-Q")


# ---------------------------------------------------------------------------
# AC #1 — ordered sections starting at 0
# ---------------------------------------------------------------------------


def test_detect_returns_ordered_sections(html_10k: bytes) -> None:
    detector = SectionDetector("10-K")
    sections = detector.detect(html_10k)
    orders = [s.order for s in sections]
    assert orders == list(range(len(sections)))
    assert orders[0] == 0


# ---------------------------------------------------------------------------
# AC #2 — 10-K core items detected with non-empty text
# ---------------------------------------------------------------------------


def test_10k_core_items_detected(html_10k: bytes) -> None:
    detector = SectionDetector("10-K")
    sections = detector.detect(html_10k)
    keys = {s.item_key for s in sections}
    for core in ("1", "1A", "7"):
        assert core in keys, f"Missing core item key {core!r}"
    for s in sections:
        assert s.text, f"Section {s.item_key!r} has empty text"


# ---------------------------------------------------------------------------
# AC #3 — 10-Q part disambiguation
# ---------------------------------------------------------------------------


def test_10q_part_disambiguation(html_10q: bytes) -> None:
    detector = SectionDetector("10-Q")
    sections = detector.detect(html_10q)
    keys = {s.item_key for s in sections}
    assert "PI-1" in keys, "Missing Part-I Item 1"
    assert "PII-1" in keys, "Missing Part-II Item 1"
    # They must be distinct sections
    pi1 = next(s for s in sections if s.item_key == "PI-1")
    pii1 = next(s for s in sections if s.item_key == "PII-1")
    assert pi1.text != pii1.text


def test_10q_metadata_includes_part(html_10q: bytes) -> None:
    detector = SectionDetector("10-Q")
    sections = detector.detect(html_10q)
    for s in sections:
        assert "part" in s.metadata, f"Section {s.item_key!r} missing 'part' in metadata"
        assert s.metadata["part"] in ("I", "II")
    pi_sections = [s for s in sections if s.item_key.startswith("PI-")]
    pii_sections = [s for s in sections if s.item_key.startswith("PII-")]
    assert all(s.metadata["part"] == "I" for s in pi_sections)
    assert all(s.metadata["part"] == "II" for s in pii_sections)


# ---------------------------------------------------------------------------
# AC #4 — letter-suffix items are distinct
# ---------------------------------------------------------------------------


def test_letter_suffix_items_distinct(html_10k: bytes) -> None:
    detector = SectionDetector("10-K")
    sections = detector.detect(html_10k)
    keys = {s.item_key for s in sections}
    assert "1" in keys
    assert "1A" in keys
    assert "7" in keys
    assert "7A" in keys
    # Verify they are different sections with different text
    item1 = next(s for s in sections if s.item_key == "1")
    item1a = next(s for s in sections if s.item_key == "1A")
    item7 = next(s for s in sections if s.item_key == "7")
    item7a = next(s for s in sections if s.item_key == "7A")
    assert item1.text != item1a.text
    assert item7.text != item7a.text


# ---------------------------------------------------------------------------
# AC #5 — tables stripped from text and counted
# ---------------------------------------------------------------------------


def test_tables_stripped_from_text_and_counted(html_10k: bytes) -> None:
    detector = SectionDetector("10-K")
    sections = detector.detect(html_10k)
    item7 = next(s for s in sections if s.item_key == "7")
    # HTML table content must not appear
    assert "<table" not in item7.text.lower()
    assert "<tr" not in item7.text.lower()
    assert "<td" not in item7.text.lower()
    # Table count tracked in metadata
    assert item7.metadata["tables_stripped"] >= 1, "Expected at least 1 table stripped in Item 7"
    # Sections without tables should have count 0
    item1 = next(s for s in sections if s.item_key == "1")
    assert item1.metadata["tables_stripped"] == 0


# ---------------------------------------------------------------------------
# AC #6 — text normalization (\xa0, zero-width, whitespace collapse)
# ---------------------------------------------------------------------------


def test_text_normalization(html_10k: bytes) -> None:
    detector = SectionDetector("10-K")
    sections = detector.detect(html_10k)
    for s in sections:
        assert "\xa0" not in s.text, f"Non-breaking space in {s.item_key!r}"
        assert "\x00" not in s.text, f"Null byte (TABLE placeholder) in {s.item_key!r}"
        assert "  " not in s.text, f"Double space in {s.item_key!r}"
        assert not s.text.startswith(" ")
        assert not s.text.endswith(" ")


# ---------------------------------------------------------------------------
# AC #7 — O7 fallback: single "unstructured" section
# ---------------------------------------------------------------------------


def test_o7_fallback_single_unstructured_section(html_minimal: bytes) -> None:
    detector = SectionDetector("10-K")
    sections = detector.detect(html_minimal)
    assert len(sections) == 1
    s = sections[0]
    assert s.name == "unstructured"
    assert s.item_key == "unstructured"
    assert s.order == 0
    assert s.metadata == {"detection": "fallback_unstructured"}
    assert s.char_count > 0
    assert s.text


# ---------------------------------------------------------------------------
# AC #8 — case-insensitive and punctuation-tolerant matching
# ---------------------------------------------------------------------------


def test_case_insensitive_and_punctuation_tolerant(html_10k: bytes) -> None:
    detector = SectionDetector("10-K")
    sections = detector.detect(html_10k)
    keys = {s.item_key for s in sections}
    # "ITEM 7A." (ALL CAPS) and "Item&nbsp;1A" (non-breaking space) must be detected
    assert "7A" in keys, "ITEM 7A (all caps) not detected"
    assert "1A" in keys, "Item&nbsp;1A (non-breaking space) not detected"


# ---------------------------------------------------------------------------
# AC #9 — TOC false positive guard
# ---------------------------------------------------------------------------


def test_toc_false_positive_guard(html_10k: bytes) -> None:
    detector = SectionDetector("10-K")
    sections = detector.detect(html_10k)
    # Each section must have substantial text — TOC entries (short stubs) filtered out
    for s in sections:
        assert s.char_count >= 200, (
            f"Section {s.item_key!r} has only {s.char_count} chars — "
            "likely a TOC false positive that slipped through"
        )


# ---------------------------------------------------------------------------
# AC #10 — char_count == len(text) for every section
# ---------------------------------------------------------------------------


def test_char_count_equals_len_text(html_10k: bytes, html_10q: bytes) -> None:
    for html, form in [(html_10k, "10-K"), (html_10q, "10-Q")]:
        detector = SectionDetector(form)  # type: ignore[arg-type]
        for s in detector.detect(html):
            assert s.char_count == len(s.text), (
                f"{form} section {s.item_key!r}: char_count={s.char_count} != len(text)={len(s.text)}"
            )


# ---------------------------------------------------------------------------
# Section is a proper Pydantic model
# ---------------------------------------------------------------------------


def test_section_is_frozen(html_10k: bytes) -> None:
    detector = SectionDetector("10-K")
    sections = detector.detect(html_10k)
    s = sections[0]
    with pytest.raises(ValidationError):
        s.name = "mutated"  # type: ignore[misc]


def test_section_fields_present(html_10k: bytes) -> None:
    detector = SectionDetector("10-K")
    sections = detector.detect(html_10k)
    for s in sections:
        assert isinstance(s, Section)
        assert isinstance(s.name, str)
        assert isinstance(s.item_key, str)
        assert isinstance(s.order, int)
        assert isinstance(s.text, str)
        assert isinstance(s.char_count, int)
        assert isinstance(s.metadata, dict)


# ---------------------------------------------------------------------------
# Integration test — skip by default; set RUN_INTEGRATION=1 to run
# ---------------------------------------------------------------------------

_skip_integration = pytest.mark.skipif(
    not os.getenv("RUN_INTEGRATION"),
    reason="set RUN_INTEGRATION=1 to run live integration tests",
)


@_skip_integration
@pytest.mark.integration
@pytest.mark.asyncio
async def test_integration_real_10k_detection() -> None:
    """Fetch a real Apple 10-K from SEC/R2 and verify SectionDetector structural invariants.

    Writes Apple 10-K HTML to R2 on cache miss; subsequent runs read from R2.
    Requires live credentials (Settings) and RUN_INTEGRATION=1.
    """
    settings = get_settings()
    async with EdgarClient(settings) as client:
        filings = await client.list_filings("0000320193", form_types=("10-K",))
        assert len(filings) >= 1, "Expected at least one Apple 10-K filing"
        meta = filings[0]  # most recent — SEC JSON is newest-first
        html = await client.fetch_primary_doc(meta)

    detector = SectionDetector("10-K")
    sections = detector.detect(html)

    # Must not fall back to O7 on a well-formed real 10-K
    assert not any(s.item_key == "unstructured" for s in sections), (
        "O7 fallback triggered on real Apple 10-K — detector failed to find 3 core items"
    )
    assert len(sections) >= 3

    # Core items present
    keys = {s.item_key for s in sections}
    assert {"1", "1A", "7"} <= keys, f"Missing core items; found: {keys}"

    # Contiguous ordering from 0
    orders = [s.order for s in sections]
    assert orders[0] == 0
    assert orders == list(range(len(sections)))

    # Every section has non-empty normalized text and char_count invariant
    for s in sections:
        assert s.text, f"Section {s.item_key!r} has empty text"
        assert s.char_count == len(s.text), (
            f"Section {s.item_key!r}: char_count={s.char_count} != len(text)={len(s.text)}"
        )


# ---------------------------------------------------------------------------
# D2 (#20) — char_count invariant raises ValueError (not AssertionError)
# ---------------------------------------------------------------------------


def test_char_count_invariant_raises_value_error(
    monkeypatch: pytest.MonkeyPatch, html_10k: bytes
) -> None:
    """D2: a char_count/len(text) mismatch raises ValueError (assert was replaced).

    The guard is defensive — detect() always sets char_count=len(text), so we
    monkeypatch Section to return a copy with char_count bumped by one, forcing the
    mismatch on the structured path (html_10k yields >= 3 core items)."""
    import alphalens.etl.sections as secmod

    real_section = secmod.Section

    def _mismatched_section(**kwargs: Any) -> Section:
        sec = real_section(**kwargs)
        return sec.model_copy(update={"char_count": sec.char_count + 1})

    monkeypatch.setattr(secmod, "Section", _mismatched_section)
    with pytest.raises(ValueError, match="char_count"):
        secmod.SectionDetector("10-K").detect(html_10k)
