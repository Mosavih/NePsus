# Self-improvement mechanism (2026-09-05): the system grades itself and the
grades change the system.

Three ledgers (all append-only JSON/SQLite, all human-readable):

1. SCORE LEDGER (implemented v1 with scenario work): every composed post
   records {pid, rubric scores per dimension, qc flags, model tier used,
   latency}. Purpose: trend lines per dimension (is Persian improving?
   which tier writes better hooks?). REVIEW: weekly, by human + agent.

2. FAILURE AUTOPSY (implemented v1): every NOT OK / gatefail / revert logs
   {stage, reason, gate detail}. A monthly job aggregates top reasons; any
   reason recurring >=3 times MUST become either a prompt rule, a threshold
   change, or a code fix — filed as a commit, not a note. (This doc exists
   because P35's guard_ok=False had no detail; the detail field was the
   first autopsy output.)

3. CALIBRATION LEDGER (reserved for Lane C forecasts): every forecast call
   records {indicator, horizon, predicted range, as_of}; a settler scores it
   against realized values monthly. Rule: no track record = no predictions;
   calibration below threshold demotes the lane automatically (fallback to
   scenario-only posts).

What this is NOT: automatic prompt rewriting (too easy to drift into
sycophancy — every change from autopsy ships as a reviewed commit), and
never silent threshold relaxation (thresholds only tighten automatically on
measured dup/guide failures; loosening needs a human).

v1 wiring: score_ledger table + record_score() in eval/score_ledger.py;
autopsy aggregation query documented here, automated in the monthly review.
