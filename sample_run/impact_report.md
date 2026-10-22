# Impact report

Simulator seed `oua-practice`. Population: every viewer who triggered during the replay day (863 agent arm, 883 holdout). The unit is the viewer, the unit that was randomized.

## Headline: arm vs arm (intent-to-treat)

- First goal reached (goals chosen by identical rules in both arms; holdout goals are shadow goals): agent 9.9% (n=845) vs holdout 9.4% (n=874): **+0.6 pts**, 95% CI [-2.2, +3.4]
- The interval includes zero: no effect can be claimed from this run.
- Inconclusive goals (window runs past observed data) excluded: agent 0, holdout 0.

Goal-independent outcomes (same comparison, no goal definitions involved):

- played_7d: agent 66.9% (n=863) vs holdout 64.7% (n=883): **+2.2 pts**, 95% CI [-2.3, +6.6]
- completed_7d: agent 44.8% (n=863) vs holdout 43.1% (n=883): **+1.7 pts**, 95% CI [-3.0, +6.3]
- purchased_14d: agent 4.1% (n=863) vs holdout 4.1% (n=883): **-0.0 pts**, 95% CI [-1.9, +1.8]

## By goal (arm vs arm, decided goals only)

| Goal | Agent | Holdout | Diff (pts) | 95% CI |
|---|---|---|---|---|
| G01 | 32.8% (n=61) | 28.3% (n=53) | +4.5 | [-12.4, +21.4] |
| G02 | 13.1% (n=145) | 8.5% (n=129) | +4.6 | [-2.7, +11.9] |
| G04 | 0.0% (n=1) | 0.0% (n=1) | +0.0 | [+0.0, +0.0] |
| G05 | 3.7% (n=54) | 5.3% (n=57) | -1.6 | [-9.2, +6.1] |
| G07 | 2.6% (n=467) | 3.6% (n=503) | -1.0 | [-3.2, +1.2] |
| G08 | 18.4% (n=49) | 22.8% (n=57) | -4.4 | [-19.8, +10.9] |
| G09 | 25.0% (n=12) | 16.7% (n=12) | +8.3 | [-24.0, +40.7] |
| G12 | 34.5% (n=55) | 32.8% (n=61) | +1.8 | [-15.5, +19.0] |

Goal mix by arm (should be similar, since goal choice ignores the arm):

```
{
 "agent": {
  "G01": 61,
  "G02": 145,
  "G03": 1,
  "G04": 1,
  "G05": 54,
  "G07": 467,
  "G08": 49,
  "G09": 12,
  "G12": 55,
  "none": 18
 },
 "holdout": {
  "G01": 53,
  "G02": 129,
  "G04": 1,
  "G05": 57,
  "G06": 1,
  "G07": 503,
  "G08": 57,
  "G09": 12,
  "G12": 61,
  "none": 9
 }
}
```

## Why simpler comparisons mislead

Viewers who were *served* (actually saw an agent page) are the ones who came back to the app while a page was live. Coming back is itself a sign of engagement, so comparing them with anyone else measures who they are, not what the agent did.

- Served vs everyone else, played within 7d: served 77.1% (n=617) vs others 59.5% (n=1129): **+17.6 pts**, 95% CI [+13.2, +22.0]
- Agent arm served vs agent arm not served: served 77.1% (n=617) vs not served 41.1% (n=246): **+36.1 pts**, 95% CI [+29.1, +43.1]
- Compare with the honest arm-vs-arm number for the same metric: +2.2 pts.

## The earlier pilot, re-analysed

- Report's method (served vs all others): served 40.1% (n=217) vs others 17.7% (n=3674): **+22.4 pts**, 95% CI [+15.8, +29.1]
- Arm vs arm: treatment 18.2% (n=1912) vs holdout 19.6% (n=1979): **-1.3 pts**, 95% CI [-3.8, +1.2]
- Treatment viewers *not* served vs holdout: not served 15.5% (n=1695) vs holdout 19.6% (n=1979): **-4.1 pts**, 95% CI [-6.5, -1.6]. Serving pulled the engaged viewers out of the treatment arm, which is why the leftovers look worse than holdout.

The pilot's 2.3x uplift is selection, not effect. Arm vs arm, the pilot agent showed no benefit, and the recommendation to roll it out to every viewer is not supported.
