"""AlphaLens v8 — Section Detector.

Parses raw 10-K / 10-Q HTML bytes into ordered, typed narrative sections.
No DB writes, no chunking, no embeddings — pure HTML → list[Section].
"""

from __future__ import annotations

import re
from typing import Any, Literal

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict

# ---------------------------------------------------------------------------
# Item maps
# ---------------------------------------------------------------------------

_10K_ITEMS: dict[str, str] = {
    "1": "Item 1. Business",
    "1A": "Item 1A. Risk Factors",
    "1B": "Item 1B. Unresolved Staff Comments",
    "1C": "Item 1C. Cybersecurity",
    "2": "Item 2. Properties",
    "3": "Item 3. Legal Proceedings",
    "4": "Item 4. Mine Safety Disclosures",
    "5": "Item 5. Market for Registrant's Common Equity",
    "6": "Item 6. Selected Financial Data",
    "7": "Item 7. Management's Discussion and Analysis",
    "7A": "Item 7A. Quantitative and Qualitative Disclosures About Market Risk",
    "8": "Item 8. Financial Statements and Supplementary Data",
    "9": "Item 9. Changes in and Disagreements With Accountants",
    "9A": "Item 9A. Controls and Procedures",
    "9B": "Item 9B. Other Information",
    "9C": "Item 9C. Disclosure Regarding Foreign Jurisdictions",
    "10": "Item 10. Directors, Executive Officers and Corporate Governance",
    "11": "Item 11. Executive Compensation",
    "12": "Item 12. Security Ownership",
    "13": "Item 13. Certain Relationships and Related Transactions",
    "14": "Item 14. Principal Accountant Fees and Services",
    "15": "Item 15. Exhibits and Financial Statement Schedules",
}

# Part-I items for 10-Q; item_key prefix "PI-"
_10Q_PART_I_ITEMS: dict[str, str] = {
    "1": "Item 1. Financial Statements",
    "2": "Item 2. Management's Discussion and Analysis",
    "3": "Item 3. Quantitative and Qualitative Disclosures About Market Risk",
    "4": "Item 4. Controls and Procedures",
}

# Part-II items for 10-Q; item_key prefix "PII-"
_10Q_PART_II_ITEMS: dict[str, str] = {
    "1": "Item 1. Legal Proceedings",
    "1A": "Item 1A. Risk Factors",
    "2": "Item 2. Unregistered Sales of Equity Securities",
    "3": "Item 3. Defaults Upon Senior Securities",
    "4": "Item 4. Mine Safety Disclosures",
    "5": "Item 5. Other Information",
    "6": "Item 6. Exhibits",
}

_10K_CORE_ITEMS: frozenset[str] = frozenset({"1", "1A", "7"})
_10Q_CORE_ITEMS: frozenset[str] = frozenset({"PI-1", "PI-2", "PII-1"})

_TABLE_PLACEHOLDER = "\x00TABLE\x00"

# Matches "Item 1A.", "ITEM  7A —", "Item\xa01A.", "item1b:" etc.
# Group 1 captures the raw item number (digits + optional letter suffix).
_ITEM_RE: re.Pattern[str] = re.compile(
    r"(?:(?:^|(?<=\s))item[\s\xa0]{0,4})(\d{1,2}[A-Za-z]?)[\s\xa0]{0,3}[.\-—–:·]?(?=\s|$)",
    re.IGNORECASE | re.MULTILINE,
)

# Matches "PART I", "Part II", "PART III", "Part IV"
_PART_RE: re.Pattern[str] = re.compile(
    r"(?:^|(?<=\s))part[\s\xa0]+(I{1,3}|IV)(?:\b|[\s\xa0])",
    re.IGNORECASE | re.MULTILINE,
)

_MIN_BODY_CHARS: int = 200


# ---------------------------------------------------------------------------
# Public models
# ---------------------------------------------------------------------------


class Section(BaseModel):
    """One detected filing section with cleaned narrative text."""

    model_config = ConfigDict(frozen=True)

    name: str
    item_key: str
    order: int
    text: str
    char_count: int
    metadata: dict[str, Any]


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _strip_and_annotate(html: bytes) -> str:
    """Strip scripts/styles/tables from HTML; return raw text with TABLE placeholders."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all(["script", "style"]):
        tag.decompose()
    for table in soup.find_all("table"):
        table.replace_with(_TABLE_PLACEHOLDER)
    return soup.get_text(separator=" ")


def _normalize(raw: str) -> str:
    """Remove TABLE placeholders, decode non-breaking/zero-width whitespace, collapse runs."""
    text = raw.replace(_TABLE_PLACEHOLDER, " ")
    # \xa0 (non-breaking space) → regular space
    text = text.replace("\xa0", " ")
    # Zero-width chars
    for zw in ("​", "‌", "‍", "﻿"):
        text = text.replace(zw, "")
    # Collapse whitespace runs
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _count_tables_in_span(raw: str, start: int, end: int) -> int:
    """Count TABLE placeholders in raw[start:end] — gives per-section table count."""
    return raw[start:end].count(_TABLE_PLACEHOLDER)


def _roman_to_part(roman: str) -> str:
    """Convert roman numeral (I/II/III/IV) to Arabic string for internal tracking."""
    mapping = {"i": "I", "ii": "II", "iii": "III", "iv": "IV"}
    return mapping.get(roman.lower(), roman.upper())


def _scan_candidates(raw: str, form_type: Literal["10-K", "10-Q"]) -> list[tuple[int, str, str]]:
    """Return (offset, item_key, label) for each Item boundary found in raw text.

    For 10-Q, item_key is prefixed with the current Part ("PI-" / "PII-").
    Candidates are ordered by document offset.
    """
    # Build a merged event list: (offset, kind, payload)
    # kind = "item" | "part"
    events: list[tuple[int, str, str]] = []

    for m in _ITEM_RE.finditer(raw):
        events.append((m.start(), "item", m.group(1).upper()))

    if form_type == "10-Q":
        for m in _PART_RE.finditer(raw):
            roman = m.group(1)
            events.append((m.start(), "part", _roman_to_part(roman)))

    events.sort(key=lambda e: e[0])

    current_part = "I"
    results: list[tuple[int, str, str]] = []

    for offset, kind, payload in events:
        if kind == "part":
            current_part = payload
            continue

        # kind == "item"
        num = payload  # already uppercased, e.g. "1A"
        if form_type == "10-K":
            label = _10K_ITEMS.get(num)
            if label is None:
                continue
            item_key = num
        else:
            if current_part in ("I", "III"):  # Part I (III not standard but guard it)
                label = _10Q_PART_I_ITEMS.get(num)
                if label is None:
                    continue
                item_key = f"PI-{num}"
            else:  # Part II / IV
                label = _10Q_PART_II_ITEMS.get(num)
                if label is None:
                    continue
                item_key = f"PII-{num}"

        results.append((offset, item_key, label))

    return results


def _apply_toc_guard(
    candidates: list[tuple[int, str, str]],
    text: str,
    min_body_chars: int = _MIN_BODY_CHARS,
) -> list[tuple[int, str, str]]:
    """Discard candidates whose span to the next candidate is too short (TOC rows)."""
    if not candidates:
        return []
    filtered: list[tuple[int, str, str]] = []
    n = len(candidates)
    for i, (offset, item_key, label) in enumerate(candidates):
        end = candidates[i + 1][0] if i + 1 < n else len(text)
        body_len = end - offset
        if body_len >= min_body_chars:
            filtered.append((offset, item_key, label))
    return filtered


# ---------------------------------------------------------------------------
# Public class
# ---------------------------------------------------------------------------


class SectionDetector:
    """Hybrid (text-pattern + DOM) parser of raw 10-K/10-Q HTML into ordered narrative sections."""

    def __init__(self, form_type: Literal["10-K", "10-Q"]) -> None:
        if form_type not in ("10-K", "10-Q"):
            raise ValueError(f"Unsupported form_type {form_type!r}. Must be '10-K' or '10-Q'.")
        self._form_type = form_type
        self._core_items = _10K_CORE_ITEMS if form_type == "10-K" else _10Q_CORE_ITEMS

    def detect(self, html: bytes) -> list[Section]:
        """Return narrative sections ordered by `order`. Falls back to a single 'unstructured'
        Section (O7) when fewer than 3 core Items are confidently located."""
        raw = _strip_and_annotate(html)
        candidates = _scan_candidates(raw, self._form_type)
        filtered = _apply_toc_guard(candidates, raw)

        found_keys = {item_key for _, item_key, _ in filtered}
        if len(found_keys & self._core_items) < 3:
            full_text = _normalize(raw)
            return [
                Section(
                    name="unstructured",
                    item_key="unstructured",
                    order=0,
                    text=full_text,
                    char_count=len(full_text),
                    metadata={"detection": "fallback_unstructured"},
                )
            ]

        sections: list[Section] = []
        n = len(filtered)
        for i, (offset, item_key, label) in enumerate(filtered):
            end = filtered[i + 1][0] if i + 1 < n else len(raw)
            tables_stripped = _count_tables_in_span(raw, offset, end)
            text = _normalize(raw[offset:end])

            meta: dict[str, Any] = {"tables_stripped": tables_stripped}
            if self._form_type == "10-Q":
                meta["part"] = "I" if item_key.startswith("PI-") else "II"

            section = Section(
                name=label,
                item_key=item_key,
                order=i,
                text=text,
                char_count=len(text),
                metadata=meta,
            )
            if section.char_count != len(section.text):
                raise ValueError(
                    f"section {item_key!r} char_count {section.char_count} "
                    f"!= len(text) {len(section.text)}"
                )
            sections.append(section)

        return sections
