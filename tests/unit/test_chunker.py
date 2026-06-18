"""Unit tests for src/alphalens/etl/chunker.py -- S7 Chunker.

All unit tests are offline: trivial injected stand-ins replace spaCy and the
nomic tokenizer so no model downloads are required (AC#12, D-S7.3).

Integration test at the bottom (skipped unless RUN_INTEGRATION=1) pipes real
fixture HTML through SectionDetector -> Chunker with live models and asserts
structural invariants only -- no hard-coded token or chunk counts.
"""

from __future__ import annotations

import os
from itertools import groupby
from typing import Any

import pytest

from alphalens.etl.chunker import Chunker, TokenCounter
from alphalens.etl.sections import Section

# ---------------------------------------------------------------------------
# Stand-in callables (deterministic, no downloads)
# ---------------------------------------------------------------------------

# 1 token per whitespace-delimited word; empty string -> 0
_word_count: TokenCounter = lambda text: len(text.split()) if text.strip() else 0  # noqa: E731


def _line_splitter(text: str) -> list[str]:
    """Split on newlines; each non-empty line is one sentence."""
    return [line.strip() for line in text.splitlines() if line.strip()]


# Shorthand for building a Chunker with stand-ins
def _chunker(target: int = 10, overlap: int = 3) -> Chunker:
    return Chunker(
        target_tokens=target,
        overlap_tokens=overlap,
        count_tokens=_word_count,
        split_sentences=_line_splitter,
    )


# Shorthand for building a minimal Section
def _section(
    text: str,
    item_key: str = "1A",
    order: int = 0,
    metadata: dict[str, Any] | None = None,
) -> Section:
    meta: dict[str, Any] = metadata if metadata is not None else {"tables_stripped": 0}
    return Section(
        name=f"Item {item_key}",
        item_key=item_key,
        order=order,
        text=text,
        char_count=len(text),
        metadata=meta,
    )


# ---------------------------------------------------------------------------
# AC#1 -- empty input
# ---------------------------------------------------------------------------


def test_empty_input_returns_empty_list() -> None:
    assert _chunker().chunk_sections([]) == []


def test_section_with_empty_text_returns_no_chunks() -> None:
    chunks = _chunker().chunk_sections([_section("")])
    assert chunks == []


def test_section_with_whitespace_only_returns_no_chunks() -> None:
    chunks = _chunker().chunk_sections([_section("   \n  \n  ")])
    assert chunks == []


# ---------------------------------------------------------------------------
# AC#2 -- token_count == count_tokens(text)
# ---------------------------------------------------------------------------


def test_token_count_equals_counter_applied_to_text() -> None:
    text = "\n".join(
        [
            "one two three four five",
            "six seven eight nine ten",
            "eleven twelve thirteen fourteen fifteen",
        ]
    )
    chunks = _chunker(target=10, overlap=3).chunk_sections([_section(text)])
    for chunk in chunks:
        assert chunk.token_count == _word_count(chunk.text), (
            f"token_count mismatch: stored={chunk.token_count} "
            f"recount={_word_count(chunk.text)} text={chunk.text!r}"
        )


# ---------------------------------------------------------------------------
# AC#3 -- no chunk spans two sections
# ---------------------------------------------------------------------------


def test_no_cross_section_chunks() -> None:
    sA = _section("alpha beta gamma delta epsilon", item_key="1", order=0)
    sB = _section("zeta eta theta iota kappa", item_key="2", order=1)
    chunks = _chunker(target=5, overlap=1).chunk_sections([sA, sB])

    section_names = {c.section for c in chunks}
    assert "Item 1" in section_names
    assert "Item 2" in section_names

    for chunk in chunks:
        if chunk.section == "Item 1":
            assert "zeta" not in chunk.text
        else:
            assert "alpha" not in chunk.text


# ---------------------------------------------------------------------------
# AC#4 -- oversized single sentence gets its own chunk, flagged
# ---------------------------------------------------------------------------


def test_oversized_sentence_emitted_as_own_chunk() -> None:
    # 12 words > target of 10
    big = "one two three four five six seven eight nine ten eleven twelve"
    chunks = _chunker(target=10, overlap=3).chunk_sections([_section(big)])
    assert len(chunks) == 1
    assert chunks[0].metadata.get("oversized") is True
    assert chunks[0].text == big


def test_oversized_sentence_not_split_mid_sentence() -> None:
    # Two lines: one oversized, one normal
    text = "a b c d e f g h i j k\nnormal sentence here"
    chunks = _chunker(target=10, overlap=3).chunk_sections([_section(text)])
    # Oversized sentence must appear as a complete, unmodified chunk
    assert any(c.metadata.get("oversized") for c in chunks)
    oversized = [c for c in chunks if c.metadata.get("oversized")]
    assert oversized[0].text == "a b c d e f g h i j k"


def test_normal_sentence_not_flagged_oversized() -> None:
    text = "short line\nanother short line"
    chunks = _chunker(target=10, overlap=3).chunk_sections([_section(text)])
    assert all(not c.metadata.get("oversized") for c in chunks)


# ---------------------------------------------------------------------------
# S05a AC#5 -- pathological input: no sentence boundaries, single long run
# ---------------------------------------------------------------------------


def test_pathological_no_sentence_breaks_capped() -> None:
    """S05a AC#5: one long run with no sentence breaks > cap; every output chunk <= cap."""
    long_run = " ".join(f"word{i}" for i in range(100))  # 100 words
    chunker = Chunker(
        target_tokens=10,
        max_tokens=20,
        count_tokens=_word_count,
        split_sentences=lambda text: [text],  # simulates spaCy returning XBRL blob as one sentence
    )
    chunks = chunker.chunk_sections([_section(long_run)])
    assert len(chunks) > 0
    for chunk in chunks:
        assert chunk.token_count <= 20, (
            f"chunk has {chunk.token_count} tokens > cap=20: {chunk.text!r}"
        )


def test_pathological_all_pieces_flagged_oversized() -> None:
    """S05a: all pieces from a recursively-split oversized sentence carry oversized=True."""
    long_run = " ".join(f"word{i}" for i in range(30))  # 30 words > target=10
    chunker = Chunker(
        target_tokens=10,
        max_tokens=15,
        count_tokens=_word_count,
        split_sentences=lambda text: [text],
    )
    chunks = chunker.chunk_sections([_section(long_run)])
    assert all(chunk.metadata.get("oversized") is True for chunk in chunks), (
        "every piece of a recursively-split oversized sentence must be flagged oversized"
    )


# ---------------------------------------------------------------------------
# AC#5 -- overlap is sentence-granular, trailing sentences only
# ---------------------------------------------------------------------------


def test_consecutive_chunks_overlap_by_trailing_sentences() -> None:
    # 4 lines x 4 words; target=8 packs 2 sentences per chunk; overlap=4 keeps last sentence.
    # chunk 0 = lines[0] + lines[1] (8 tokens); overlap_sent = lines[1] (4 tokens <= overlap=4)
    # chunk 1 starts at lines[1], so lines[1] appears verbatim at the start of chunk 1.
    lines = [
        "one two three four",
        "five six seven eight",
        "nine ten eleven twelve",
        "thirteen fourteen fifteen sixteen",
    ]
    text = "\n".join(lines)
    chunks = _chunker(target=8, overlap=4).chunk_sections([_section(text)])

    assert len(chunks) >= 2
    # The overlap sentence (lines[1]) must appear in both chunk 0 and chunk 1
    overlap_sent = lines[1]
    assert overlap_sent in chunks[0].text, f"{overlap_sent!r} not in chunk 0: {chunks[0].text!r}"
    assert chunks[1].text.startswith(overlap_sent), (
        f"Chunk 1 should start with overlap sentence {overlap_sent!r}, got: {chunks[1].text!r}"
    )


def test_overlap_does_not_exceed_overlap_tokens() -> None:
    lines = [f"word{i}a word{i}b word{i}c word{i}d" for i in range(6)]
    text = "\n".join(lines)
    chunks = _chunker(target=8, overlap=4).chunk_sections([_section(text)])
    # overlap region = sentences shared between consecutive chunks
    for idx in range(len(chunks) - 1):
        c0_sents = set(chunks[idx].text.splitlines())
        c1_sents = set(chunks[idx + 1].text.splitlines())
        shared = c0_sents & c1_sents
        shared_tokens = sum(_word_count(s) for s in shared)
        assert shared_tokens <= 4, f"Overlap tokens {shared_tokens} > overlap_tokens=4"


# ---------------------------------------------------------------------------
# AC#6 -- overlap never crosses section boundary
# ---------------------------------------------------------------------------


def test_overlap_never_crosses_section_boundary() -> None:
    sA = _section(
        "alpha beta gamma delta\nepsilon zeta eta theta",
        item_key="1",
        order=0,
    )
    sB = _section(
        "iota kappa lambda mu\nnu xi omicron pi",
        item_key="2",
        order=1,
    )
    chunks = _chunker(target=4, overlap=2).chunk_sections([sA, sB])

    first_b = next(c for c in chunks if c.section == "Item 2")
    # First chunk of section B must not contain any text from section A
    assert "alpha" not in first_b.text
    assert "epsilon" not in first_b.text


# ---------------------------------------------------------------------------
# AC#7 -- chunk_index 0-based per section; section_order == Section.order
# ---------------------------------------------------------------------------


def test_chunk_index_resets_per_section() -> None:
    sA = _section("a b c d e\nf g h i j\nk l m n o", item_key="1", order=0)
    sB = _section("p q r s t\nu v w x y\nz aa bb cc dd", item_key="2", order=1)
    chunks = _chunker(target=5, overlap=0).chunk_sections([sA, sB])

    for section_name, group in groupby(chunks, key=lambda c: c.section):
        group_list = list(group)
        indices = [c.chunk_index for c in group_list]
        assert indices == list(range(len(indices))), (
            f"chunk_index not contiguous in section {section_name!r}: {indices}"
        )


def test_section_order_equals_source_section_order() -> None:
    sA = _section("first line here", item_key="1", order=5)
    sB = _section("second line here", item_key="2", order=12)
    chunks = _chunker(target=10, overlap=0).chunk_sections([sA, sB])

    for chunk in chunks:
        if chunk.section == "Item 1":
            assert chunk.section_order == 5
        else:
            assert chunk.section_order == 12


# ---------------------------------------------------------------------------
# AC#8 -- no mid-sentence breaks (splitter boundaries respected)
# ---------------------------------------------------------------------------


def test_no_mid_sentence_breaks() -> None:
    # Each line is exactly one sentence; chunks must align to line boundaries
    lines = [
        "the cat sat on the mat",
        "the dog ran down the lane",
        "the bird flew over the hill",
        "the fish swam in the pond",
    ]
    text = "\n".join(lines)
    chunks = _chunker(target=12, overlap=6).chunk_sections([_section(text)])

    valid_starts = set(lines)
    for chunk in chunks:
        first_line = chunk.text.split(" ", 6)
        # Verify chunk.text starts with a known sentence, not a fragment
        chunk_starts_with_known = any(chunk.text.startswith(s) for s in valid_starts)
        assert chunk_starts_with_known, f"Chunk starts mid-sentence: {chunk.text!r}"
    _ = first_line  # suppress unused warning


# ---------------------------------------------------------------------------
# AC#9 -- unstructured fallback (item_key == "unstructured")
# ---------------------------------------------------------------------------


def test_unstructured_fallback_section_name() -> None:
    section = Section(
        name="unstructured",
        item_key="unstructured",
        order=0,
        text="line one here\nline two here\nline three here",
        char_count=40,
        metadata={"tables_stripped": 0, "detection": "fallback_unstructured"},
    )
    chunks = _chunker(target=5, overlap=2).chunk_sections([section])
    assert len(chunks) > 0
    for chunk in chunks:
        assert chunk.section == "unstructured"
        assert chunk.metadata["item_key"] == "unstructured"


# ---------------------------------------------------------------------------
# AC#10 -- section <= target -> exactly one chunk with chunk_index == 0
# ---------------------------------------------------------------------------


def test_small_section_produces_single_chunk() -> None:
    # 3 words total, target=10 -> fits in one chunk
    section = _section("one two three")
    chunks = _chunker(target=10, overlap=3).chunk_sections([section])
    assert len(chunks) == 1
    assert chunks[0].chunk_index == 0
    assert not chunks[0].metadata.get("oversized")


def test_section_exactly_at_target_produces_single_chunk() -> None:
    # 10 words = target=10
    section = _section("a b c d e f g h i j")
    chunks = _chunker(target=10, overlap=3).chunk_sections([section])
    assert len(chunks) == 1
    assert chunks[0].chunk_index == 0


# ---------------------------------------------------------------------------
# AC#11 -- metadata propagation
# ---------------------------------------------------------------------------


def test_metadata_always_carries_item_key() -> None:
    section = _section("some text here", item_key="7")
    chunks = _chunker(target=10, overlap=3).chunk_sections([section])
    for chunk in chunks:
        assert "item_key" in chunk.metadata
        assert chunk.metadata["item_key"] == "7"


def test_metadata_no_part_key_for_10k_section() -> None:
    # 10-K sections do not have a "part" key in metadata
    section = _section("some text here", item_key="1A")
    chunks = _chunker(target=10, overlap=3).chunk_sections([section])
    for chunk in chunks:
        assert "part" not in chunk.metadata


def test_metadata_propagates_part_for_10q_section() -> None:
    section = Section(
        name="Item 2. MD&A",
        item_key="PI-2",
        order=3,
        text="revenue increased\nexpenses decreased\nmargin improved",
        char_count=50,
        metadata={"tables_stripped": 0, "part": "I"},
    )
    chunks = _chunker(target=5, overlap=2).chunk_sections([section])
    assert len(chunks) > 0
    for chunk in chunks:
        assert chunk.metadata.get("part") == "I"
        assert chunk.metadata.get("item_key") == "PI-2"


# ---------------------------------------------------------------------------
# AC#12 -- fully usable with injected stand-ins (no model download)
# ---------------------------------------------------------------------------


def test_dependency_injection_no_model_download() -> None:
    call_log: list[str] = []

    def _counting_counter(text: str) -> int:
        call_log.append(f"count:{text[:10]}")
        return len(text.split())

    def _counting_splitter(text: str) -> list[str]:
        call_log.append("split")
        return [line.strip() for line in text.splitlines() if line.strip()]

    chunker = Chunker(
        target_tokens=5,
        overlap_tokens=2,
        count_tokens=_counting_counter,
        split_sentences=_counting_splitter,
    )
    section = _section("hello world\nfoo bar baz")
    chunks = chunker.chunk_sections([section])

    assert len(chunks) > 0
    assert any("split" in entry for entry in call_log)
    assert any("count" in entry for entry in call_log)


# ---------------------------------------------------------------------------
# Integration test -- skip by default; set RUN_INTEGRATION=1 to run
# Requires: python -m spacy download en_core_web_sm
# ---------------------------------------------------------------------------

_skip_integration = pytest.mark.skipif(
    not os.getenv("RUN_INTEGRATION"),
    reason="set RUN_INTEGRATION=1 to run live integration tests",
)


@_skip_integration
@pytest.mark.integration
def test_integration_real_10k_chunking(html_10k: bytes) -> None:
    """Pipe fixture 10-K HTML through SectionDetector -> Chunker with real models.

    Asserts structural invariants only -- no hard-coded chunk or token counts.
    """
    from alphalens.etl.sections import SectionDetector  # noqa: PLC0415

    sections = SectionDetector("10-K").detect(html_10k)
    chunker = Chunker()  # real nomic tokenizer + spaCy splitter

    chunks = chunker.chunk_sections(sections)

    assert len(chunks) > 0, "Expected at least one chunk from 10-K fixture"

    for chunk in chunks:
        # AC#2: stored token_count must match a fresh recount
        assert chunk.token_count == chunker._count(chunk.text), (
            f"token_count mismatch on chunk {chunk.chunk_index} of section {chunk.section!r}"
        )
        assert chunk.text.strip() != "", "Empty chunk text"
        assert chunk.chunk_index >= 0

    # AC#7: chunk_index is 0-based and contiguous within each section
    for _key, group in groupby(chunks, key=lambda c: (c.section, c.section_order)):
        indices = [c.chunk_index for c in group]
        assert indices == list(range(len(indices))), f"chunk_index not contiguous: {indices}"

    # AC#4: oversized chunks must not be empty
    oversized = [c for c in chunks if c.metadata.get("oversized")]
    for c in oversized:
        assert c.text.strip() != "", "Oversized chunk has empty text"

    # AC#11: every chunk carries item_key in metadata
    for chunk in chunks:
        assert "item_key" in chunk.metadata, (
            f"item_key missing from chunk metadata: {chunk.metadata}"
        )
