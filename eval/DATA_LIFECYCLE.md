# Data lifecycle (2026-09-05): every measurement has a biography.

States a row moves through (status column + friends):

  INGESTED (status NULL) — fresh from a provider fetch (WDI/OWID/IMF/Yahoo/
  SCI) or Gate-1 extraction. Each ingest logs provider + fetch date; rows are
  content-addressed by (indicator, reference_period, measurement_source,
  subject) so reruns never duplicate (ux_measurements_series).

  PROVISIONAL (status='provisional') — provider-flagged estimates (2025 OWID
  vintages) or second-hand figures (SCI-via-press). Provisional rows are
  usable but must travel with «برآورد» labeling; the composer freshness rule
  treats them as estimates, never observations.

  VERIFIED — provisional row superseded by the provider's finalized value.
  Supersede is UPDATE-in-place + a row in supersede_log {indicator, period,
  old_value, new_value, at} (TO WIRE — the current gap: refresh overwrites
  silently, so nobody can see revisions). Readers never see this table; it
  powers autopsies ("which posts cited the revised value?").

  QUARANTINED — rows/claims proven wrong or off-scope (17 bogus
  interventions). Never deleted (provenance), never linked (levers-only
  dossier filter enforces). status='quarantined' + reason.

  RETIRED — problems/claims demoted (gates-rejected → candidate-never-retry;
  stale ready posts). Kept for dedup memory (the same-story judge reads
  them), never composed.

Integrity net (already live): data_integrity_ok() blocks compose on
corruption; repair_owid fixes known vintages; claim_check verifies post
numbers against the DB after writing.

Freshness contract: monthly-precision rows exempt the <5-point stub-skip
(the P29 lesson: sparse ≠ stale). A series whose latest point is older than
its natural cadence is "stale", not wrong — the composer says the year,
never an apology paragraph (2026-09-05 rule).
