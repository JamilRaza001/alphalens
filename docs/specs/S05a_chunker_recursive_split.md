\# Spec — Chunker Recursive Split + Hard Token Cap (extends Spec 05)

\## Goal

The chunker emits one oversized chunk (~9750 tokens) for iXBRL/XML filings whose

text the sentence-aware splitter fails to break, causing Jina to reject the embed

batch with HTTP 400 (INPUT\_TOKEN\_LIMIT\_EXCEEDED). Make splitting recursive and

bounded so every emitted chunk is guaranteed at or below a configurable maximum,

regardless of input structure — fixing the 400s universally while preserving

natural boundaries (and thus retrieval quality) wherever possible.

\## Function Signatures

(Recon src/alphalens/etl/chunker.py for exact types; public entry point unchanged.)

class Chunker:

def chunk\_sections(self, sections: list\[Section\]) -> list\[Chunk\]:

"""Unchanged contract. Now guarantees every Chunk.token\_count <= settings.chunk\_max\_tokens."""

def \_split\_recursive(self, text: str, max\_tokens: int) -> list\[str\]:

"""New helper. Split by descending separators (paragraph -> sentence ->

word -> raw token) until each piece <= max\_tokens. Raw token cut = last resort."""

\# config.py Settings gains:

chunk\_max\_tokens: int = 512 # hard ceiling; far below Jina's 8194 = tokenizer-mismatch headroom

\## Acceptance Criteria

1\. No Chunk from chunk\_sections has token\_count > settings.chunk\_max\_tokens, for ALL 151

filings including the 7 iXBRL 10-Qs (JPM/MSFT/V).

2\. Splitting prefers natural boundaries (paragraph -> sentence -> word); raw token-level

cut only when a single run still exceeds the cap.

3\. Normal text unchanged: ~400-token target + 50-token overlap preserved; the 144

currently-working filings chunk equivalently (no regression).

4\. token\_count uses the tokenizer the chunker already uses; cap (512) sits far below

Jina's 8194 so chunker-vs-Jina tokenizer differences cannot cause a 400.

5\. Unit test: a pathological input (one long run, no sentence punctuation, > cap) yields

every output chunk <= cap.

6\. Section-awareness preserved: chunks never span across major sections.

7\. Re-running the 7 previously-failed filings produces 0 embed-400s; all reach 'processed'.

\## Gotchas

\- Tokenizer mismatch: chunker counts with nomic/HF tokenizer, Jina with its own. Keeping

the cap (512) well under 8194 makes the mismatch irrelevant — do NOT set the cap near 8194.

\- iXBRL quality: until the parser fix (deferred), these 7 chunk via the token-level

fallback, so their chunks are semantically rougher (mid-content cuts). Acceptable for v1

— fixes the crash; parser fix later improves coherence.

\- spaCy may treat a giant table as one "sentence" — the recursive fallback must never

assume sentence-splitting reduces size; it must fall through to word/token level.

\## Forward-compatibility

Chunks keep filing\_id, section, section\_order, chunk\_index — enough to reconstruct a

chunk's full parent section later (filing\_id + section, ordered by chunk\_index), enabling

parent-document retrieval with NO re-ingest.
