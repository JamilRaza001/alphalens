"""AlphaLens v8 -- API wire types (S18).

WIRE TYPES ONLY. Every class here is a boundary shape: what crosses the HTTP surface between
the agent and the S19 frontend. There is no transport logic (that is ``app.py``) and no agent
logic (that is ``nodes.py`` -- S18/D1 is wrap-only).

The only behaviour in this module is PURE agent-state -> wire conversion (``from_state`` /
``from_citation`` / ``from_pair``). It lives here rather than in ``app.py`` so the mapping that
AC8 and AC9 pin can be tested without an ASGI app, and so there is exactly ONE place where a
live ``AgentState`` key becomes a wire field.

Recon note (S18 G4, 13 Aug 2026): these shapes are reconciled against ``agent/state.py`` at
HEAD, NOT against the ``run_query.py`` footer's printed form. Four fields of the original
doc-sourced spec were stale -- see the spec's Amendment section. Live code is ground truth.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from alphalens.agent.state import AgentState, Citation


class QueryRequest(BaseModel):
    """Request body for ``POST /query``.

    Deliberately carries NO request/trace/correlation id field (S18 Q12). ``request_id`` is
    generated SERVER-side with ``uuid4()``, mirroring run_query.py's intake (S16 G4) so the HTTP
    surface and the CLI harness produce ids the same way. A client-supplied id is a
    trace-poisoning surface: the caller could collide or forge ids and corrupt log correlation,
    and nothing downstream validates it. ``user_id`` is a caller-supplied attribution field, not
    an id we trust for tracing.
    """

    question: str = Field(min_length=1, max_length=2000)
    user_id: str | None = None


class GapCell(BaseModel):
    """Wire form of a live ``tuple[str, int]`` (ticker, year) cell.

    ``AgentState.coverage_gaps`` / ``.capacity_drops`` are ``list[tuple[str, int]]``
    (state.py:199, :204). JSON has no tuple, and a positional 2-element array forces every
    consumer to remember which slot is which -- so the pair is named on the wire (S18 Q3).
    """

    ticker: str
    year: int

    @classmethod
    def from_pair(cls, pair: tuple[str, int]) -> GapCell:
        """Convert one live ``(ticker, year)`` state tuple into its wire form."""
        ticker, year = pair
        return cls(ticker=ticker, year=year)


class MetaEvent(BaseModel):
    """Payload of the ``meta`` SSE event -- emitted ONCE, before the first answer token.

    Mirrors the run_query.py footer keys (37e4b41) so the CLI and the HTTP surface never drift
    into reporting different things about the same run -- see AC8 for the exact intersection
    contract and its two documented exceptions (``request_id`` is meta-only; ``latency`` is
    footer-only and rides on ``DoneEvent.latency_s``).

    ``capacity_drops`` is a write-only v1 state key: ``evaluate_node`` writes it and NO node
    reads it (state.py:204). It is carried here for footer parity only. The key that IS consumed
    -- ``dropped_for_capacity`` (state.py:191, read by ``synthesize_node``'s honesty rail) -- is
    deliberately NOT on the wire (S18 Q4 / Out of Scope).
    """

    request_id: str
    confidence: str  # "low" | "high"
    # Live Literal is ["coverage", "llm", "none"] (state.py:198) -- there is no "capacity"
    # member; the spec's original fourth value was invented. Typed `str` on the wire so a future
    # state-side addition cannot break serialisation.
    confidence_reason: str
    coverage_gaps: list[GapCell]
    capacity_drops: list[GapCell]
    unavailable_tickers: list[str]
    unavailable_years: list[int]
    unavailable_companies: list[str]
    plan_tickers: list[str]  # `[]` when query_plan is unset (AC9)

    @classmethod
    def from_state(cls, state: AgentState, request_id: str) -> MetaEvent:
        """Build the pre-answer metadata from a returned ``AgentState`` (AC8, AC9).

        Key access mirrors run_query.py:67-80 exactly, which is what makes footer parity a
        property of the code rather than of a comment: every field is read straight off the
        state EXCEPT ``query_plan``, which an early exit can leave unset and which the harness
        therefore also reads defensively.

        AC9 -- ``plan_tickers`` normalises to ``[]``, never ``None``. This deliberately diverges
        from the CLI, which prints ``None``: a wire contract that changes type on an edge case
        forces every consumer into a null check.
        """
        plan = state.get("query_plan")
        return cls(
            request_id=request_id,
            confidence=state["confidence"],
            confidence_reason=state["confidence_reason"],
            coverage_gaps=[GapCell.from_pair(p) for p in state["coverage_gaps"]],
            capacity_drops=[GapCell.from_pair(p) for p in state["capacity_drops"]],
            unavailable_tickers=list(state["unavailable_tickers"]),
            unavailable_years=list(state["unavailable_years"]),
            unavailable_companies=list(state["unavailable_companies"]),
            plan_tickers=list(plan.tickers) if plan is not None else [],
        )


class CitationOut(BaseModel):
    """One entry of the ``citations`` SSE event -- emitted ONCE, after the last token.

    Field-identical to the live ``Citation`` (state.py:136); kept as a separate wire type so a
    future agent-side change to ``Citation`` surfaces here as a compile-time mapping decision
    rather than as a silent contract change for S19.
    """

    chunk_id: str
    ticker: str
    filing_type: str
    period_year: int
    section: str | None

    @classmethod
    def from_citation(cls, citation: Citation) -> CitationOut:
        """Convert one agent-side ``Citation`` into its wire form."""
        return cls(
            chunk_id=citation.chunk_id,
            ticker=citation.ticker,
            filing_type=citation.filing_type,
            period_year=citation.period_year,
            section=citation.section,
        )


class DoneEvent(BaseModel):
    """Payload of the terminal ``done`` event -- fires only on a successful run (see ErrorEvent).

    ``token_count`` is the number of ``token`` EVENTS this stream emitted (S18 Q11) -- i.e. how
    many non-empty deltas ``stream_synthesis`` yielded, or how many chunks ``degraded_stream``
    produced on the degraded path. It is NOT a model-token count and NOT a billing figure:
    Groq's tokenizer boundaries, this stream's ``data:`` frames, and any usage metering are three
    different things, and one delta routinely spans several model tokens. Named ``token_count``
    because it counts the events literally named ``token``.
    """

    latency_s: float
    token_count: int
    # Breaker state at terminal time, resolved AFTER the drain -- not at meta time (S18 Q1).
    # This reports what the BREAKER was, NOT whether fallback content was served (N2); the
    # CLOSED-boundary gap is documented in the spec's Out of Scope and deferred to v2.
    breaker_open: bool


class ErrorEvent(BaseModel):
    """Payload of the ``error`` event. Emitted IN-BAND (S18 G2) -- the HTTP status is already 200
    by the time the stream is live, so failures cannot use a status code.

    ``message`` is built as ``f"{type(exc).__name__} during {phase}"`` and NEVER incorporates
    ``str(exc)``: that is precisely where DB URLs, DSNs and traceback fragments leak into a
    client-visible payload.
    """

    message: str  # operator-safe summary; NEVER a raw traceback or DB URL
    phase: str  # "graph" | "stream"
    # N1: `error` is terminal and `done` never fires -- the breaker signal rides here. Same
    # contract as DoneEvent.breaker_open: breaker state at terminal time, NOT a claim that
    # fallback content was served (N2).
    breaker_open: bool
