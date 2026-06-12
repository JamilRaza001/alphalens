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
        max_tokens: int = 512,
        count_tokens: TokenCounter | None = None,
        split_sentences: SentenceSplitter | None = None,
    ) -> None:
        self._target = target_tokens
        self._overlap = overlap_tokens
        self._max_tokens = max_tokens
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

    def _split_recursive(self, text: str, max_tokens: int) -> list[str]:
        """Split text by descending separators until every piece <= max_tokens.

        Order: paragraph (\\n\\n, \\n) → sentence (self._split) → word packing →
        binary character split (last resort; always terminates).
        """
        if self._count(text) <= max_tokens:
            return [text] if text.strip() else []

        # 1. Paragraph separators
        for sep in ("\n\n", "\n"):
            parts = [p.strip() for p in text.split(sep) if p.strip()]
            if len(parts) > 1:
                out: list[str] = []
                for p in parts:
                    out.extend(self._split_recursive(p, max_tokens))
                return out

        # 2. Sentence split — guard: splitter may return [text] unchanged (e.g. spaCy on XBRL blob)
        sents = self._split(text)
        if len(sents) > 1:
            out = []
            for s in sents:
                out.extend(self._split_recursive(s, max_tokens))
            return out

        # 3. Word-level greedy packing
        words = text.split()
        if len(words) > 1:
            out = []
            bucket: list[str] = []
            bucket_tok = 0
            for word in words:
                wt = self._count(word)
                if wt > max_tokens:
                    if bucket:
                        out.append(" ".join(bucket))
                        bucket, bucket_tok = [], 0
                    out.extend(self._split_recursive(word, max_tokens))
                elif bucket_tok + wt > max_tokens:
                    out.append(" ".join(bucket))
                    bucket, bucket_tok = [word], wt
                else:
                    bucket.append(word)
                    bucket_tok += wt
            if bucket:
                out.append(" ".join(bucket))
            return out

        # 4. Binary character split — last resort; guaranteed to reduce size each call
        if len(text) <= 1:
            return [text] if text.strip() else []
        mid = len(text) // 2
        out = []
        for half in (text[:mid].strip(), text[mid:].strip()):
            if half:
                out.extend(self._split_recursive(half, max_tokens))
        return out

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
            # Recursively split to <= self._max_tokens; flag all pieces as oversized.
            if sent0_tokens > self._target:
                over_meta: dict[str, Any] = {**base_meta, "oversized": True}
                for piece in self._split_recursive(sentences[i], self._max_tokens):
                    chunks.append(
                        Chunk(
                            text=piece,
                            token_count=self._count(piece),
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
