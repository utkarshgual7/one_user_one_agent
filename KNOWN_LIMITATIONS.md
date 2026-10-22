# Scope and operating notes

The implementation is deliberately bounded to the supplied replay, simulator, and policy surface. The following
operating notes describe those boundaries and the interpretation of the measured results.

1. **Deterministic model provider.** The shipped provider returns the first rule-eligible goal and exercises the
   fault paths (timeout, 503, malformed output, unsupported claims) and call budget. A production provider can use
   the same `advise()` interface and strict parsing and eligibility checks.

2. **No measurable effect.** Arm vs arm, first goal reached is +0.6 pts (95% CI −2.2 to +3.4) on the practice seed,
   and +0.6 to +2.2 across three seeds. Every interval includes zero. With about 860 viewers per arm, the run can
   only detect effects of roughly ±3 pts or more.

3. **G07 (resume series) is the most common goal but rarely reached** (about 3% in both arms). The simulator picks
   titles from the dialect pool rather than the page, so the target series is hard to influence. Goal results are
   reported by goal type so this limitation remains visible rather than being hidden by an aggregate.

4. **Confidence is a hand-set heuristic** (start at 0.8, minus fixed amounts for conflicts, stale or unknown status,
   thin pages, purchase goals). It describes how sound the evidence is, not a calibrated success probability.

5. **Content ranking is simple**: popularity from past plays, plus the viewer's top genre, free-or-premium ordering
   by goal stage, and short first episodes for G02. There is no collaborative filtering. The brief rewards decisions
   over recommendation accuracy.

6. **Learning is narrow.** Per-viewer notes record verdicts, organic versus exposed successes, and undeliverable
   channels. Only the last one changes later decisions (that channel is avoided). Nothing is learned across viewers.

7. **The judging horizon is fixed** at the simulator's end (`judge.OBSERVED_UNTIL`, 2026-10-15 IST midnight). Runs
   for a different replay day should set that horizon to the corresponding simulator end.

8. **The latency model is partly simulated.** Compute time is measured. Dependency waits and the 4-worker queue are
   modelled, because the replay runs faster than real time.

9. **Withdrawal repair removes the withdrawn title and drops rows that fall below three titles.** This keeps the
   repaired page valid without introducing a title that was not part of the original decision.

10. **Message scope:** the replay uses push and WhatsApp follow-ups. In-app messages, per-viewer time zones, and the
    `wa_trial_ending_v1` and `wa_new_episode_v2` templates are outside this replay's evidence and policy surface.

11. **Single process, single SQLite file.** Fine for 5,000 viewers. See ARCHITECTURE.md for the 10M-viewer shape.
