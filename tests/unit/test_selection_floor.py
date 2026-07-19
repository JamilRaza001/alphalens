"""Unit tests for `select_with_floor` (S17 -- cell-aware per-pair selection).

Pure, fakes-only: `select_with_floor` does no I/O, so every case builds in-memory
`ScoredChunk`s and asserts on the returned `(selected, dropped)` tuple. No live
Groq / DB / reranker. Covers AC3-AC8 of docs/specs/S17_selection_floor.md.
"""

from __future__ import annotations

from alphalens.agent.nodes import select_with_floor
from alphalens.agent.state import RetrievedChunk, ScoredChunk


def _sc(chunk_id: str, ticker: str, year: int, score: float) -> ScoredChunk:
    """A minimal ScoredChunk carrying only the fields selection reads (pair key + score)."""
    return ScoredChunk(
        chunk=RetrievedChunk(
            chunk_id=chunk_id,
            text="evidence",
            section="Item 7",
            ticker=ticker,
            period_year=year,
            filing_type="10-K",
        ),
        rerank_score=score,
    )


def _pairs(selected: list[ScoredChunk]) -> list[tuple[str, int]]:
    return [(sc.chunk.ticker, sc.chunk.period_year) for sc in selected]


def _count(selected: list[ScoredChunk], ticker: str, year: int) -> int:
    return sum(1 for p in _pairs(selected) if p == (ticker, year))


# ── AC3 ──────────────────────────────────────────────────────────────────────
def test_floor_guarantees_every_pair() -> None:
    # Two pairs, ample cap: each non-empty pair contributes exactly min(floor, len), the rest
    # of the budget fills by global score, total <= cap, nothing dropped.
    scored = (
        [_sc(f"a{i}", "AAPL", 2023, 9.0 - i) for i in range(5)]  # 5 high-scoring AAPL
        + [_sc(f"m{i}", "MSFT", 2023, 3.0 - i) for i in range(3)]  # 3 lower-scoring MSFT
    )
    selected, dropped = select_with_floor(scored, floor_per_pair=2, max_context_chunks=20)

    assert dropped == []
    assert _count(selected, "AAPL", 2023) >= 2
    assert (
        _count(selected, "MSFT", 2023) >= 2
    )  # floor guarantees MSFT its slots despite lower scores
    assert len(selected) == 8 <= 20  # nothing dropped; all fit
    # Final order is best-first.
    assert selected == sorted(selected, key=lambda s: s.rerank_score, reverse=True)


# ── AC4 ──────────────────────────────────────────────────────────────────────
def test_empty_cell_reclaim_frees_slots() -> None:
    # A thin pair (fewer than floor candidates) reserves only what it has; the freed slots go
    # to the fill pool rather than being wasted.
    scored = (
        [_sc(f"a{i}", "AAPL", 2023, 9.0 - i) for i in range(5)]  # 5 AAPL
        + [_sc("m0", "MSFT", 2023, 4.0)]  # 1 MSFT (< floor of 3)
    )
    selected, dropped = select_with_floor(scored, floor_per_pair=3, max_context_chunks=6)

    assert dropped == []
    assert _count(selected, "MSFT", 2023) == 1  # reserved only what it had
    assert _count(selected, "AAPL", 2023) == 5  # freed MSFT slot reclaimed by AAPL fill
    assert len(selected) == 6  # cap fully used, no slot wasted


# ── AC5 ──────────────────────────────────────────────────────────────────────
def test_graduated_reduction_keeps_all_pairs() -> None:
    # n_pairs * floor > cap but n_pairs <= cap: depth drops to max(1, cap // n_pairs), ALL
    # pairs stay covered, nothing dropped.
    scored = [
        _sc(f"{t}{i}", t, 2023, score - i)
        for t, score in (("AAPL", 9.0), ("MSFT", 6.0), ("GOOGL", 3.0))
        for i in range(5)  # 5 chunks per pair
    ]
    selected, dropped = select_with_floor(scored, floor_per_pair=5, max_context_chunks=6)

    assert dropped == []
    # effective_floor = max(1, 6 // 3) = 2 -> each pair exactly 2, total 6.
    assert _count(selected, "AAPL", 2023) == 2
    assert _count(selected, "MSFT", 2023) == 2
    assert _count(selected, "GOOGL", 2023) == 2
    assert len(selected) == 6


# ── AC6 ──────────────────────────────────────────────────────────────────────
def test_hard_overflow_drops_and_reports() -> None:
    # n_pairs > cap: exactly `cap` pairs kept (ranked by best per-pair score), each gets 1 slot,
    # every dropped pair is reported.
    scored = [
        _sc(f"{t}0", t, 2023, score)
        for t, score in (("AAPL", 9.0), ("MSFT", 8.0), ("GOOGL", 7.0), ("AMZN", 6.0))
    ]
    selected, dropped = select_with_floor(scored, floor_per_pair=2, max_context_chunks=2)

    assert len(selected) == 2  # exactly cap
    # Top-2 by best score kept; bottom-2 dropped.
    assert set(_pairs(selected)) == {("AAPL", 2023), ("MSFT", 2023)}
    assert dropped == [("AMZN", 2023), ("GOOGL", 2023)]  # sorted, both reported


# ── AC8 ──────────────────────────────────────────────────────────────────────
def test_s31_shape_msft_survives() -> None:
    # The actual bug: AAPL-heavy pool, one thin MSFT pair, margins within ~0.1. Under the old
    # global top-8 slice AAPL took all 8 and MSFT was wiped out; the floor must guarantee MSFT
    # its pair >= 1 chunk.
    scored = [_sc(f"a{i}", "AAPL", 2024, 2.2051 - i * 0.001) for i in range(7)] + [
        _sc("m0", "MSFT", 2024, 2.0891)  # loses the last global slot by ~0.116
    ]
    selected, dropped = select_with_floor(scored, floor_per_pair=2, max_context_chunks=8)

    assert dropped == []
    assert _count(selected, "MSFT", 2024) >= 1  # 7:1 wipeout cannot recur


# ── AC7 ──────────────────────────────────────────────────────────────────────
def test_determinism_stable_ties() -> None:
    # All-equal scores: ties break by chunk_id, and identical inputs (any order) yield identical
    # output ordering.
    forward = [_sc(f"c{i}", "AAPL", 2023, 5.0) for i in range(6)]
    shuffled = list(reversed(forward))

    out_a, drop_a = select_with_floor(forward, floor_per_pair=2, max_context_chunks=4)
    out_b, drop_b = select_with_floor(shuffled, floor_per_pair=2, max_context_chunks=4)

    ids_a = [sc.chunk.chunk_id for sc in out_a]
    ids_b = [sc.chunk.chunk_id for sc in out_b]
    assert ids_a == ids_b  # order-independent -> deterministic
    assert ids_a == ["c0", "c1", "c2", "c3"]  # tie-break ascending by chunk_id
    assert drop_a == drop_b == []
