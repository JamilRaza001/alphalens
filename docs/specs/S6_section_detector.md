## Spec S6 — Section Detector (v8 spec 04)

> Module: `src/alphalens/etl/sections.py`
> Depends on: S5 (`EdgarClient.fetch_primary_doc` supplies cached HTML bytes)
> Build order: Phase 1.A, after S5, before S7 (chunker)
> Resolves: **O7** (section-detection fallback for non-standard filings)

---

### Goal

Parse the raw 10-K / 10-Q HTML (as delivered by the S5 EDGAR client / R2 cache) into a list of typed, ordered **narrative** sections following SEC "Item" structure — Item 1 (Business), Item 1A (Risk Factors), Item 7 (MD&A), Item 8 (Financial Statements), etc. The detector is **form-type aware**: a 10-K is a flat list of Items, while a 10-Q is split into Part I (Financial Information) and Part II (Other Information) where Item numbers repeat across parts and must be disambiguated. Detection uses a **hybrid** strategy — BeautifulSoup4 + lxml extract and clean the text first, then text-pattern matching locates Item boundaries on the normalized text (HTML structure alone is too inconsistent across filers/years to rely on). Tables, scripts, and styles are stripped: v1 retrieval is narrative-only vector search; structured financial numbers are XBRL/v2 (`financial_facts`). Output feeds the S7 chunker, which uses section boundaries for section-aware chunking and metadata filtering at retrieval time. This module does **not** chunk, embed, or write to the DB.

### Function Signatures

```python
from typing import Literal
from pydantic import BaseModel


class Section(BaseModel):
    """One detected filing section with cleaned narrative text."""
    name: str          # Canonical label, e.g. "Item 1A. Risk Factors"
    item_key: str      # Normalized key. 10-K: "1A". 10-Q: "PII-1A" (part-qualified)
    order: int         # 0-based position in the document
    text: str          # Cleaned narrative text (tables/scripts/styles stripped, whitespace normalized)
    char_count: int    # len(text)
    metadata: dict      # e.g. {"tables_stripped": 3, "part": "II"} | {"detection": "fallback_unstructured"}


class SectionDetector:
    """Hybrid (text-pattern + DOM) parser of raw 10-K/10-Q HTML into ordered narrative sections."""

    def __init__(self, form_type: Literal["10-K", "10-Q"]) -> None:
        """Select the Item map for the filing type. Raises ValueError on unsupported form_type."""

    def detect(self, html: bytes) -> list[Section]:
        """Return narrative sections ordered by `order`. Falls back to a single 'unstructured'
        Section (O7) when fewer than 3 core Items are confidently located."""
```

### Acceptance Criteria

1. `detect()` accepts raw HTML **bytes** and returns `list[Section]` ordered by `order` ascending, starting at `0`.
2. For a standard 10-K, locates the core narrative Items — at minimum **Item 1 (Business)**, **Item 1A (Risk Factors)**, and **Item 7 (MD&A)** — each with non-empty `text`.
3. For a 10-Q, **Part I and Part II are tracked separately** so repeated Item numbers do not collide (Part I Item 1 ≠ Part II Item 1); `item_key` is part-qualified (`"PI-1"`, `"PII-1A"`) and `metadata["part"]` is set.
4. Letter-suffixed Items are parsed distinctly: "Item 7A" ≠ "Item 7", "Item 1B" ≠ "Item 1A" ≠ "Item 1".
5. All `<table>`, `<script>`, and `<style>` content is removed from `text`; `metadata["tables_stripped"]` records the count of tables removed within that section's span.
6. `text` is normalized: no residual HTML tags; `\xa0`, zero-width, and other non-breaking whitespace → single spaces; runs of whitespace collapsed; leading/trailing trimmed.
7. **O7 fallback:** if fewer than 3 core Items are confidently located, return a **single** `Section` with `name="unstructured"`, `item_key="unstructured"`, the full cleaned body as `text`, and `metadata={"detection": "fallback_unstructured"}`.
8. Boundary matching is case-insensitive and tolerant of whitespace/punctuation between the word and number (matches "ITEM 1A.", "Item&nbsp;1A", "Item 1A —").
9. Table-of-Contents false positives are avoided: a candidate Item header only opens a section if it is followed by substantial body text (e.g. ≥ a configurable minimum char count), so the TOC's link list does not create empty/duplicate sections.
10. `char_count == len(text)` for every returned `Section`.
11. Unsupported `form_type` (anything not in `{"10-K", "10-Q"}`) raises `ValueError` at construction, before any parsing.

### Gotchas

- **HTML structure is unreliable.** The same Item header may appear as a heading tag, a bold `<p>`, or a styled `<div>` depending on the filer and year. Do **not** key off tag types — extract clean text first, then pattern-match Item boundaries on the normalized text.
- **10-Q repeats Item numbers across parts.** Both Part I and Part II have an "Item 1" / "Item 1A". Without Part context the second occurrence overwrites or merges with the first. Track the current Part as you scan and qualify `item_key` accordingly.
- **The Table of Contents lies.** Near the top, the TOC lists every "Item 1A. Risk Factors" as a link — these match the same pattern as the real headers and would create spurious/empty sections. Require substantial following body text before opening a section (AC #9), or anchor on the *last* qualifying occurrence.
