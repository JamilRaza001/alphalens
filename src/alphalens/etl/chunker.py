"""AlphaLens v8 -- Chunker (S7).

Converts list[Section] into token-aware, sentence-aware list[Chunk] objects.
No DB writes, no embeddings -- pure list[Section] -> list[Chunk].

Three invariants:
  1. A Chunk never spans two sections.
  2. A Chunk never starts or ends mid-sentence.
  3. token_count is measured with the same tokenizer as the downstream embedder
     (nomic-embed-text-v1.5 WordPiece, add_special_tokens=False).

DB schema notes (S9 concern):
  - Chunk.section_order has no matching column in chunks table yet; S9 migration adds it.
  - Chunk.metadata has no matching JSONB column yet; S9 migration adds it.
  - Chunk.chunk_index is per-section here; S9 re-indexes globally before DB upsert.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict

from alphalens.etl.sections import Section

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

TokenCounter = Callable[[str], int]
SentenceSplitter = Callable[[str], list[str]]

# ---------------------------------------------------------------------------
# Module-level lazy-load sentinels (populated on first factory call)
# ---------------------------------------------------------------------------

_nomic_tok: Any = None  # transformers PreTrainedTokenizerFast
_spacy_nlp: Any = None  # spacy Language


# ---------------------------------------------------------------------------
# Public models
# ---------------------------------------------------------------------------


class Chunk(BaseModel):
    """One token-bounded, sentence-aligned chunk of a filing section."""

    model_config = ConfigDict(frozen=True)

    text: str  # normalized chunk text (whole sentences only)
    token_count: int  # == count_tokens(text)
    section: str  # Section.name
    section_order: int  # Section.order
    chunk_index: int  # 0-based, contiguous within the section
    metadata: dict[str, Any]  # item_key, part (10-Q only), oversized flag


# ---------------------------------------------------------------------------
# Default factory functions (lazy-loaded, cached at module level)
# ---------------------------------------------------------------------------


def default_token_counter() -> TokenCounter:
    """Return a content-token counter backed by the nomic WordPiece tokenizer.

    Loads nomic-ai/nomic-embed-text-v1.5 once and caches it at module level.
    add_special_tokens=False so the count reflects content tokens only,
    matching what the downstream embedder sees (D-S7.2).
    """
    global _nomic_tok
    if _nomic_tok is None:
        from transformers import AutoTokenizer  # noqa: PLC0415

        _nomic_tok = AutoTokenizer.from_pretrained("nomic-ai/nomic-embed-text-v1.5")
        _log.debug("nomic tokenizer loaded")
    tok = _nomic_tok

    def _count(text: str) -> int:
        return len(tok.encode(text, add_special_tokens=False))

    return _count


def default_sentence_splitter() -> SentenceSplitter:
    """Return a sentence splitter backed by spaCy en_core_web_sm.

    Loads the model once with ner/lemmatizer/tagger disabled (segmentation only).
    Raises RuntimeError with an install hint if the model is missing (D-S7.1).
    """
    global _spacy_nlp
    if _spacy_nlp is None:
        try:
            import spacy  # noqa: PLC0415

            _spacy_nlp = spacy.load("en_core_web_sm", disable=["ner", "lemmatizer", "tagger"])
            _log.debug("spaCy en_core_web_sm loaded")
        except OSError as exc:
            raise RuntimeError(
                "spaCy model 'en_core_web_sm' not found. "
                "Install it with: python -m spacy download en_core_web_sm"
            ) from exc
    nlp = _spacy_nlp

    def _split(text: str) -> list[str]:
        return [sent.text.strip() for sent in nlp(text).sents if sent.text.strip()]

    return _split


# ---------------------------------------------------------------------------
# Chunker
# ---------------------------------------------------------------------------


class Chunker:
    """Greedy sentence-packing chunker with per-section overlap.

    Both callables are dependency-injected so unit tests can use trivial
    stand-ins without downloading any models (D-S7.3).
    """

    def __init__(
        self,
        target_tokens: int = 400,
        overlap_tokens: int = 50,
        count_tokens: TokenCounter | None = None,
        split_sentences: SentenceSplitter | None = None,
    ) -> None:
        self._target = target_tokens
        self._overlap = overlap_tokens
        self._count: TokenCounter = (
            count_tokens if count_tokens is not None else default_token_counter()
        )
        self._split: SentenceSplitter = (
            split_sentences if split_sentences is not None else default_sentence_splitter()
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def chunk_sections(self, sections: list[Section]) -> list[Chunk]:
        """Convert every section into Chunks and return them concatenated.

        chunk_index resets to 0 at the start of each section (AC#7).
        Overlap never crosses a section boundary (AC#6).
        """
        result: list[Chunk] = []
        for section in sections:
            result.extend(self._chunk_section(section))
        return result

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _chunk_section(self, section: Section) -> list[Chunk]:
        sentences = [s for s in self._split(section.text) if s]
        if not sentences:
            return []

        # Propagate item_key always; part only for 10-Q sections (AC#11)
        base_meta: dict[str, Any] = {"item_key": section.item_key}
        if "part" in section.metadata:
            base_meta["part"] = section.metadata["part"]

        chunks: list[Chunk] = []
        chunk_index = 0
        i = 0  # index of the first sentence in the next chunk window

        while i < len(sentences):
            sent0_tokens = self._count(sentences[i])

            # --- Oversized single sentence (AC#4) ---
            # Emit as its own chunk; flag metadata; never split mid-sentence.
            if sent0_tokens > self._target:
                over_meta: dict[str, Any] = {**base_meta, "oversized": True}
                chunks.append(
                    Chunk(
                        text=sentences[i],
                        token_count=sent0_tokens,
                        section=section.name,
                        section_order=section.order,
                        chunk_index=chunk_index,
                        metadata=over_meta,
                    )
                )
                chunk_index += 1
                i += 1
                continue

            # --- Greedy packing: accumulate whole sentences up to target (AC#4, AC#8) ---
            window: list[str] = []
            total_tokens = 0
            j = i

            while j < len(sentences):
                st = self._count(sentences[j])
                if total_tokens + st > self._target:
                    break
                window.append(sentences[j])
                total_tokens += st
                j += 1

            # Recount after joining to satisfy AC#2 exactly
            chunk_text = " ".join(window)
            chunks.append(
                Chunk(
                    text=chunk_text,
                    token_count=self._count(chunk_text),
                    section=section.name,
                    section_order=section.order,
                    chunk_index=chunk_index,
                    metadata=dict(base_meta),
                )
            )
            chunk_index += 1

            if j >= len(sentences):
                break  # all sentences in this section consumed

            # --- Sentence-granular leading overlap (AC#5, AC#6) ---
            # Pull whole trailing sentences from `window` until their sum
            # exceeds overlap_tokens; those sentences start the next window.
            # `skip = max(1, ...)` guarantees forward progress even when a
            # single sentence fills the entire overlap budget.
            overlap_sents: list[str] = []
            overlap_total = 0
            for sent in reversed(window):
                st = self._count(sent)
                if overlap_total + st <= self._overlap:
                    overlap_sents.insert(0, sent)
                    overlap_total += st
                else:
                    break

            skip = max(1, len(window) - len(overlap_sents))
            i += skip

        return chunks
