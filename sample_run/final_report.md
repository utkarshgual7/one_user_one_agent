# Final report: sample replay (2026-10-01 IST, simulator seed `oua-practice`)

Produced by `PYTHONPATH=src python3 -m agent run --pack pack --out sample_run`.

## Summary

- **2,679 decisions** for 1,746 viewers. 90 re-delivered triggers were ignored.
- **896 published**: 896 home pages, plus 237 push and 178 WhatsApp follow-ups scheduled for 19:00 IST the next
  day.
- **1,783 no-action decisions, each with its reason:**

| Reason | Count |
|---|---|
| Holdout arm (shadow goal recorded) | 1,324 |
| Trigger buffered offline, more than 10 min old | 185 |
| Same page already live (repeat open in one visit) | 163 |
| App dialect invalid (`null` 46, `d7` 29, `NA` 11) | 86 |
| No measurable goal eligible | 23 |
| Data store read failed twice | 2 |

- **Latency:** p50 21 ms, p95 0.82 s, max 10 s (during the data-store slowdown). **0 over 60 s, 0 over 5 min.** The
  peak hour (15:00 UTC, 340 decisions) also had p95 0.82 s.
- **Simulator policy problems: 0.** No holdout actions, no messages at night, none without consent, no cap
  violations.
- **Model:** called on 1,054 decisions (only where 2 or more goals were eligible and an action was possible). 15
  calls failed (503 outage, timeout, malformed output) and fell back to rules. 1 unsupported claim was rejected.

## Impact

See `impact_report.md`. Arm vs arm, first goal reached: agent 9.9% vs holdout 9.4%, **+0.6 pts (95% CI −2.2 to
+3.4)**. Played within 7 days: +2.2 pts (−2.3 to +6.6). Purchased within 14 days: 0.0. No effect can be claimed. On
the same data, the pilot report's method (served vs everyone else) would claim +17.6 pts.

Goal verdicts: agent 84 succeeded / 761 failed, holdout 82 / 792, 0 inconclusive. 71 of the 84 agent successes
followed a page view or message click. The other 13 are recorded as organic.

## Surprises

| Surprise | What happened |
|---|---|
| Offline burst (triggers up to 8.8 days old) | 185 recorded as no-action; their events still update history |
| Purchase arriving minutes after a decision | the live purchase settles the open goal immediately (test: `test_purchase_right_after_decision_settles_goal`) |
| Three different dialects | the app setting is used, the other two recorded as conflicts (e.g. U-04945) |
| G03 success rule corrected 08:30 | rule version bumped for G03 only; 0 open G03 goals at that time, 0 reopened, nothing else reprocessed |
| Model claims G04 eligibility "confirmed in billing" | U-01991: rejected (`status=never`); rule-chosen plan published. The other two such faults landed on triggers that were 9–15 h old, so the model was never asked |
| Catalogue descriptions with instructions (T0002, T0522) | descriptions are never read. T0002 is ordinary content and appears on pages as a normal title |
| Model unavailable 10:35–10:42 | 13 model calls failed with 503: rules only, pages published, no messages |
| Data store degraded 13:10–13:17 | 16 decisions, max 10 s. 2 read failures led to no action |
| Title T0001 withdrawn 07:00 | 1 live page (U-04907) republished without it; no messages named it |
| Evening peak | 340 decisions in the peak hour, p95 unchanged |

## Viewer walkthroughs

Full state for each is in `viewer_state/<id>.json`.

**U-04094 (agent): app open to judged success.**
1. Opens the app at 19:39 UTC; the trigger is 0 s old.
2. Last app open was 20 days ago, so G01–G07 don't apply and G12 "return after absence" is eligible. G12 is chosen
   with a 7-day deadline; success is any `play_started`.
3. The model is asked (several goals eligible) and agrees.
4. A d1 home page is built, and a WhatsApp follow-up uses the approved template.
5. All 17 checks pass. Published 821 ms after delivery.
6. The simulator shows the personalised page (`oua-026315`), then a play 5 minutes later (`oua-026316`). Verdict:
   **succeeded, after exposure**.

**U-04945 (agent): three-way dialect conflict.** App setting d4, profile d2, campaign link d6. The page is built in
d4 and the other two are recorded. Confidence drops to 0.6 with the reasons. G02 (first full episode) failed: no
completion within 72 h.

**U-01386 (holdout).** G05 shadow goal recorded, no action. The viewer bought on 6 Oct, so it succeeded. This is
exactly why holdout outcomes must be counted: some viewers convert with no agent at all.

**U-02808 (agent): stale trigger.** The app open had been buffered offline for 7 days (603,033 s). No action; the
history is updated.

**U-01991 (agent): unsupported model claim.** The model says G04 with 0.97 confidence, "confirmed in billing". The
rules say `status=never`. The claim is rejected and logged as a failed `model_claim` check. G07 (resume a d3
series) is published with a push.

**U-03051 (agent): model outage.** 503 at 10:35. Rules only: a page is published and no message is sent. 221 ms.

**U-01704 (holdout): data store failure.** Two reads failed in the degraded window, so there wasn't enough evidence
and the agent did nothing (10 s). A later trigger the same day decided normally.

**U-04907 (agent): withdrawn title.** The page published at 05:54 contained T0001. At 07:00 it was republished
without it (A-000288 → A-000360).
