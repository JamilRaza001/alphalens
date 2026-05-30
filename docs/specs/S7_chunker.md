# Spec S7 — Chunker (v8 spec 05)

**Maps to:** v8 spec 05 (`05_chunker.md`) -> `src/alphalens/etl/chunker.py`
**Depends on:** S6 `SectionDetector.detect(html) -> list[Section]`
**Feeds:** S8 (embeddings) -> upsert spec (`chunks` table)
**Scope guard:** chunking only. NO embeddings (S8), NO DB writes (upsert), NO query-side logic.

---

## Goal

Turn the `list[Section]` produced by S6 into token-aware, section-aware, sentence-aware
`Chunk` objects (~400 tokens, ~50-token overlap) that are ready to embed. A chunk is the
unit of retrieval, so chunk quality = retrieval quality. Three invariants drive the design:
(1) a chunk NEVER spans two sections — it inherits S6's clean boundaries (S6 Payoff 1);
(2) a chunk NEVER starts or ends mid-sentence — sentence integrity preserves embedding
quality; (3) chunk size is measured with the same tokenizer the embedder uses, so the
count stored in `chunks.token_count` reflects what the model actually sees (no silent
truncation downstream in S8). Output maps directly onto the `chunks` table columns
`section`, `section_order`, `chunk_index`, `text`, `token_count` (+ `metadata` JSONB);
`filing_id`, `embedding`, and `embedding_model_version` are added later by S8/upsert.

## Decisions Locked (do not re-litigate at implementation)

- **D-S7.1 (O2):** Sentence splitter = spaCy `en_core_web_sm` (statistical, abbreviation-aware).
  Rule-based `sentencizer` is rejected — it false-splits "Inc." / "U.S." / "No.". Load once,
  disable unused components (`ner`, `lemmatizer`, `tagger`) for speed. Model is a setup
  prerequisite, not a runtime download. ETL runs offline (local / GitHub Actions), so spaCy
  weight is NOT a Lambda concern (the query path embeds short queries whole, no chunking).
- **D-S7.2:** Canonical token counter = nomic tokenizer via `transformers.AutoTokenizer`
  (`nomic-ai/nomic-embed-text-v1.5`), counting content tokens (`add_special_tokens=False`).
  Aligns chunk size with the embedder. `tiktoken` rejected (OpenAI BPE != our vocab).
  `transformers`/`tokenizers` already ship transitively via `sentence-transformers` — NO new
  `pyproject.toml` dependency.
- **D-S7.3:** Both the counter and the splitter are **dependency-injected** (constructor
  params with lazy factory defaults), so unit tests run deterministic + offline with trivial
  stand-ins (whitespace counter, regex splitter) and need no model download.
- **D-S7.4:** Unstructured fallback (S6 returned one `item_key == "unstructured"` Section):
  chunk it sequentially; section-aware logic is a no-op since there is only one section.

## Function Signatures

```python
from collections.abc import Callable
from typing import Any
from pydantic import BaseModel, ConfigDict

from alphalens.etl.sections import Section

TokenCounter = Callable[[str], int]
SentenceSplitter = Callable[[str], list[str]]


class Chunk(BaseModel):
    """Immutable, embeddable unit of one section's text. Maps to `chunks` columns."""
    model_config = ConfigDict(frozen=True)

    text: str               # normalized chunk text (whole sentences only)
    token_count: int        # == count_tokens(text); fills chunks.token_count
    section: str            # Section.name; fills chunks.section
    section_order: int      # == source Section.order; fills chunks.section_order
    chunk_index: int        # 0-based, contiguous WITHIN the section; resets per section
    metadata: dict[str, Any]  # carries item_key, part, oversized flag, etc.


def default_token_counter() -> TokenCounter:
    """Lazy-load the nomic WordPiece tokenizer once; return a content-token counter."""


def default_sentence_splitter() -> SentenceSplitter:
    """Lazy-load spaCy en_core_web_sm once (segmentation only); return a splitter.
    Fail fast with an install hint if the model is missing."""


class Chunker:
    def __init__(
        self,
        target_tokens: int = 400,
        overlap_tokens: int = 50,
        count_tokens: TokenCounter | None = None,      # default: nomic tokenizer
        split_sentences: SentenceSplitter | None = None,  # default: spaCy
    ) -> None: ...

    def chunk_sections(self, sections: list[Section]) -> list[Chunk]:
        """Greedily pack whole sentences into <=target_tokens chunks, per section,
        with sentence-granular leading overlap that never crosses a section boundary."""
```

## Acceptance Criteria

1. `chunk_sections([])` returns `[]`.
2. For every output chunk, `chunk.token_count == count_tokens(chunk.text)` (self-consistent).
3. No chunk spans two sections: all chunks derived from a Section share that Section's
   `name` and `order`; no chunk text mixes sentences from different sections.
4. Each chunk satisfies `token_count <= target_tokens`, EXCEPT a single sentence that alone
   exceeds `target_tokens` — it becomes its own chunk flagged `metadata["oversized"] = True`
   (never hard-split mid-sentence).
5. Within a section, consecutive chunks overlap by whole trailing sentence(s) of the
   previous chunk summing to `<= overlap_tokens`; the shared sentence(s) appear verbatim at
   the start of the next chunk. A section yielding one chunk has no overlap.
6. Overlap never crosses a section boundary: the first chunk of every section has no leading
   overlap carried from the previous section.
7. `chunk_index` is 0-based and contiguous within each section (resets to 0 per section);
   `section_order` equals the source `Section.order`.
8. No chunk text begins or ends mid-sentence (except AC#4). With the default spaCy splitter,
   "Inc.", "U.S.", "No.", "Corp." do NOT trigger false sentence breaks.
9. Unstructured fallback: a single Section with `item_key == "unstructured"` is chunked
   sequentially without crashing; output chunks carry `section == "unstructured"`.
10. A Section whose full text is `<= target_tokens` yields exactly one chunk with
    `chunk_index == 0` and no overlap.
11. Every chunk sets `section = Section.name` and propagates `Section.metadata` keys
    `item_key` and (10-Q) `part` into `Chunk.metadata`.
12. The class is fully usable with injected `count_tokens` / `split_sentences`; unit tests
    use trivial stand-ins and require no spaCy/transformers model download.

## Gotchas

- **Overlap is sentence-granular, not token-granular.** To build overlap, pull WHOLE
  trailing sentences from the previous chunk until their token sum reaches the overlap budget
  (or the chunk runs out) — never slice a sentence to hit exactly `overlap_tokens`. Slicing
  reintroduces the mid-sentence cut that chunking exists to prevent.
- **spaCy model presence.** `default_sentence_splitter()` must raise a clear, actionable
  error (`python -m spacy download en_core_web_sm`) if the model is absent, rather than a raw
  spaCy `OSError`. Root-cause-first: a missing model is a setup gap, not a code bug.
- **Token-counter alignment.** Count content tokens with `add_special_tokens=False`; the
  embedder adds `[CLS]`/`[SEP]` per chunk (~2 tokens), so excluding them keeps the stored
  count honest. Jina v3 (API) may drift slightly from nomic's count, but 400 << 8192 context
  leaves ample margin — do not introduce a second counter.
