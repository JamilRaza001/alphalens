\# Spec (DEFERRED — Phase 1.B retrieval / v2 quality) — Parent-Document Retrieval

\## Goal

"Small-to-big": match on small precise chunks at retrieval, but pass the larger parent

section to the LLM at synthesis — full context without diluting retrieval precision.

\## Notes

\- NO schema change / NO re-ingest: parent reconstructed from chunks via (filing\_id, section)

ordered by chunk\_index/section\_order.

\- Implemented in Retrieve (Node 2) + Synthesize (Node 5), NOT the chunker.

\- Defer until the agent/retrieval layer exists (Phase 1.B) or v2 quality milestone.

\## Acceptance Criteria (draft)

1\. Retrieval ranks on small-chunk embeddings (unchanged).

2\. Before synthesis, each top chunk is expanded to its full parent section text (dedup when

several top chunks share one parent).

3\. Citations reference the precise chunk; context shown to the LLM is the parent section.

4\. End-to-end latency stays within the <=15s budget.
