"""Investigation Layer (ontology v1.10).

Separate pipeline from the news Discovery Layer. Retrieves from OpenAlex /
Crossref (free, keyless) and extracts structured Studies / Findings /
Interventions with the local LLM. Interventions are first-class objects with
persistent identity so evidence accumulates across studies.

Submodules:
  retrieval  -- OpenAlex query building + fetching
  extraction -- local-LLM Findings / Interventions / Iran-relevance
  pipeline   -- orchestration: Problem -> retrieve -> extract -> link

STATUS: v0 (implemented 2026-08-15, free stack only).

Freeze notes / known v0 limits (do NOT treat as bugs to silently "fix"):
- Retrieval is OpenAlex keyless only; Crossref/arXiv are not yet wired
  (arXiv connectivity unverified in this sandbox).
- Intervention extraction still admits a small residual of non-intervention
  concepts (e.g. "Liberal international order") from political-science
  abstracts. Prompt-tightened once; full precision would need either
  retrieval filtered to intervention/evaluation study types or fuzzy
  validation -- both deferred. Same v0 discipline as Events/Claims/Measurements.
- A Study is NOT a Claim; Findings carry graded causal_strength + limitations.
- Evidence accumulation: interventions are upserted by canonical name so
  multiple studies attach to ONE intervention row (the hub). This is the
  core value of the bidirectional model.
- Iran-relevance assessment is best-effort; may be empty if the local model
  rate-limits (429) -- pipeline degrades gracefully, never aborts.
- No fuzzy entity matching: Intervention<->Entity links (applicable_to /
  implemented_by) are not yet populated (needs the deferred entity-identity
  resolution from STRATEGY section 10).
"""
