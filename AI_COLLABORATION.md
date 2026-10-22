# AI collaboration log

## Tools

Claude Code (Claude Opus) in VS Code wrote most of the code and docs, phase by phase, under my direction. No AI
service saw the pack's viewer data except through that local session; the agent itself uses no hosted model.

## How the work was directed

The build ran in nine phases (data exploration → state → facts → goals → planner and provider → verifier →
replay → judge and impact → docs). After each phase the assistant explained what it built, I questioned it, and I
decided whether to continue. Before any code, I made it explain the measurement problem (agent vs holdout, and why
the pilot's served-vs-everyone comparison is selection bias) until I could restate it myself.

## How output was checked

- **Every phase was run against the real pack, not just unit tests.** Each phase ended with a run over all 2,769
  replay triggers, and the distributions were checked by eye.
- **Independent checks against each other.** The verifier deliberately does not import the planner. Running it over
  every real plan is how the hero-duplication bug below was found.
- **The simulator's own policy check.** The final run reports 0 policy problems: no holdout actions, no messages at
  night, no messages without consent, no cap violations.
- **Multiple simulator seeds.** The impact result was checked on three seeds before it was written up.

## AI output that was wrong and was corrected

Each of these was produced by the assistant, caught by a check, and fixed:

| What the AI did                                                                                                                                                         | How it was caught                                                    | Fix                                                                  |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------- | -------------------------------------------------------------------- |
| Parsed the campaign-link dialect from the wrong URL segment, so 100% of deeplinks looked like they "disagreed"                                                          | The 100% figure was implausible                                      | Index fixed; the real figure is 43%                                  |
| Chose G07 "resume series" for 60% of viewers, including series in a dialect their app can't show                                                                        | Distribution check on real triggers                                  | G07 now requires the series to be in the app's dialect               |
| Reported "not enough titles in d7" for viewers whose app dialect is invalid                                                                                             | Reading the reasons on real data                                     | Invalid dialects are now detected in facts, and the reason states it |
| Planner reused the first row's title as the hero, so the same show appeared twice on 259 of 645 pages                                                                   | The verifier, run over real plans                                    | Hero reserved before rows are built; test added                      |
| Agent never called the model: `choose()` stops at the first eligible goal, so "≥2 eligible" was never true                                                              | p50 latency of 20 ms was too low for a design with 0.8 s model calls | Eligible goals computed in full                                      |
| Data-store failure path returned the wrong shape and crashed the replay                                                                                                 | First full replay                                                    | Fixed                                                                |
| Goal rule versions were global, so G07 outcomes claimed rule "v2" when only G03 changed                                                                                 | Reading `outcomes.jsonl`                                             | Per-goal versions; test added                                        |
| Test fixtures assumed the wrong goal (twice). The agent was right both times: first play for a never-played viewer, then return after absence for a viewer gone 14 days | Failing tests                                                        | Fixtures fixed, not the agent                                        |
| Two tests accepted either of two outcomes, so they could not fail                                                                                                       | Review                                                               | Made strict                                                          |

## AI suggestions not accepted

The following suggestions were considered and intentionally rejected. These are scope decisions, not unfinished work:

- **Exclude the complete data pack and simulator from the submission.** The assistant initially suggested keeping the
  whole pack out to follow the privacy rule. I overruled this because evaluators need the synthetic data, simulator,
  and configuration to run the project. The pack contains generated data only; no real viewer data is included.
- **Add a hosted language-model provider.** I rejected this because it would require credentials or network access
  and would make the measured run non-reproducible. The deterministic provider exercises timeout, outage, malformed
  output, unsupported-claim, and budget paths while preserving the same strict provider interface.
- **Use the model to determine eligibility, evidence, or policy approval.** I rejected this because those decisions
  must remain deterministic and auditable. The model can only rank goals already found eligible by rules; the
  verifier independently checks the resulting plan.
- **Tune the G07 resume-series rule to increase its simulator success rate.** I rejected this because it would
  optimize for one simulator rather than represent a defensible product rule. Results remain reported by goal type,
  including G07's low success rate.
- **Add an in-app message channel to the measured replay.** I rejected this because no selected goal requires it and
  the replay's evidence and policy surface are already covered by home pages, push, and WhatsApp.
