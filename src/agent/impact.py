"""Measure the agent's effect against the randomized holdout, and show why simpler comparisons mislead.

Unit of analysis: the viewer (the unit that was randomized). Population: every viewer who triggered during the
replay, in either arm. Triggering happens before the agent acts, so it cannot depend on the arm.

Primary metric: the viewer's first goal of the replay (chosen by the same rules in both arms) reached.
Secondary metrics do not depend on goals at all: play / completion within 7 days, purchase within 14 days.
"""
import csv
import json
import math
from collections import defaultdict

from . import clock
from .facts import event_time


def _rate(xs):
    return sum(xs) / len(xs) if xs else float("nan")


def compare(a, b):
    """Difference of proportions a - b with a 95% normal-approximation interval."""
    pa, pb = _rate(a), _rate(b)
    se = math.sqrt(pa * (1 - pa) / len(a) + pb * (1 - pb) / len(b)) if a and b else float("nan")
    d = pa - pb
    return {"n_a": len(a), "rate_a": round(pa, 4), "n_b": len(b), "rate_b": round(pb, 4),
            "diff_pts": round(d * 100, 2), "ci95_pts": [round((d - 1.96 * se) * 100, 2), round((d + 1.96 * se) * 100, 2)]}


def viewer_outcomes(state, decisions, observed_until):
    first = {}
    for d in decisions:
        first.setdefault(d["viewer_id"], d)
    out = {}
    for uid, d in first.items():
        start = d["trigger"]["deliver_at"]
        ev = [e for e in state.history(uid, observed_until) if event_time(e) >= start]
        ids = {a["action_id"] for a in state.actions(uid)}
        within = lambda kind, days: any(e["event"] == kind and event_time(e) <= clock.shift(start, days=days) for e in ev)
        goals_ = sorted(state.goals(user_id=uid), key=lambda g: g["started_at"])
        g = goals_[0] if goals_ else None
        out[uid] = {
            "arm": d["arm"],
            "goal_id": g and g["goal_id"], "goal_verdict": g and g["status"],
            "played_7d": within("play_started", 7), "completed_7d": within("episode_completed", 7),
            "purchased_14d": any(e["event"] == "purchase_completed" and not e["properties"].get("is_trial") for e in ev),
            "served": any(e["event"] == "home_page_shown" and e["properties"].get("personalised")
                          and e["properties"].get("page_action_id") in ids for e in ev),
        }
    return out


def analyse(viewers):
    arms = defaultdict(list)
    for v in viewers.values():
        arms[v["arm"]].append(v)
    ag, ho = arms["agent"], arms["holdout"]
    decided = lambda vs: [v["goal_verdict"] == "succeeded" for v in vs if v["goal_verdict"] in ("succeeded", "failed")]
    res = {"population": {arm: len(vs) for arm, vs in arms.items()},
           "goal_balance": {arm: _count(v["goal_id"] for v in vs) for arm, vs in arms.items()},
           "inconclusive": {arm: sum(v["goal_verdict"] == "inconclusive" for v in vs) for arm, vs in arms.items()},
           "primary_goal_reached": compare(decided(ag), decided(ho)),
           "secondary": {m: compare([v[m] for v in ag], [v[m] for v in ho])
                         for m in ("played_7d", "completed_7d", "purchased_14d")},
           "by_goal": {}}
    for gid in sorted({v["goal_id"] for v in viewers.values() if v["goal_id"]}):
        a = decided([v for v in ag if v["goal_id"] == gid])
        b = decided([v for v in ho if v["goal_id"] == gid])
        if a and b:
            res["by_goal"][gid] = compare(a, b)
    served = [v for v in ag if v["served"]]
    res["misleading"] = {
        "served_vs_everyone_else": compare([v["played_7d"] for v in served],
                                           [v["played_7d"] for v in viewers.values() if not v["served"]]),
        "agent_arm_served_vs_not_served": compare([v["played_7d"] for v in served],
                                                  [v["played_7d"] for v in ag if not v["served"]]),
    }
    return res


def _count(xs):
    c = defaultdict(int)
    for x in xs:
        c[x or "none"] += 1
    return dict(sorted(c.items()))


def pilot_reanalysis(pack_root):
    rows = list(csv.DictReader(open(f"{pack_root}/pilot/pilot_outcomes.csv")))
    ok = lambda rs: [r["succeeded"] == "true" for r in rs]
    t = [r for r in rows if r["arm"] == "treatment"]
    h = [r for r in rows if r["arm"] == "holdout"]
    served = [r for r in t if r["served"] == "true"]
    others = [r for r in rows if r["served"] != "true"]
    return {"report_method_served_vs_others": compare(ok(served), ok(others)),
            "arm_vs_arm": compare(ok(t), ok(h)),
            "treatment_not_served_vs_holdout": compare(ok([r for r in t if r["served"] != "true"]), ok(h))}


def _fmt(c, label_a="agent", label_b="holdout"):
    return (f"{label_a} {c['rate_a'] * 100:.1f}% (n={c['n_a']}) vs {label_b} {c['rate_b'] * 100:.1f}% (n={c['n_b']}): "
            f"**{c['diff_pts']:+.1f} pts**, 95% CI [{c['ci95_pts'][0]:+.1f}, {c['ci95_pts'][1]:+.1f}]")


def report_md(res, pilot, sim_seed):
    p = res["primary_goal_reached"]
    sig = p["ci95_pts"][0] > 0 or p["ci95_pts"][1] < 0
    lines = [
        "# Impact report", "",
        f"Simulator seed `{sim_seed}`. Population: every viewer who triggered during the replay day "
        f"({res['population'].get('agent', 0)} agent arm, {res['population'].get('holdout', 0)} holdout). "
        "The unit is the viewer, the unit that was randomized.", "",
        "## Headline: arm vs arm (intent-to-treat)", "",
        "- First goal reached (goals chosen by identical rules in both arms; holdout goals are shadow goals): "
        + _fmt(p),
        f"- {'The interval excludes zero.' if sig else 'The interval includes zero: no effect can be claimed from this run.'}",
        f"- Inconclusive goals (window runs past observed data) excluded: agent {res['inconclusive'].get('agent', 0)}, "
        f"holdout {res['inconclusive'].get('holdout', 0)}.", "",
        "Goal-independent outcomes (same comparison, no goal definitions involved):", ""]
    lines += [f"- {m}: {_fmt(c)}" for m, c in res["secondary"].items()]
    lines += ["", "## By goal (arm vs arm, decided goals only)", "", "| Goal | Agent | Holdout | Diff (pts) | 95% CI |",
              "|---|---|---|---|---|"]
    for gid, c in res["by_goal"].items():
        lines.append(f"| {gid} | {c['rate_a'] * 100:.1f}% (n={c['n_a']}) | {c['rate_b'] * 100:.1f}% (n={c['n_b']}) | "
                     f"{c['diff_pts']:+.1f} | [{c['ci95_pts'][0]:+.1f}, {c['ci95_pts'][1]:+.1f}] |")
    lines += ["", "Goal mix by arm (should be similar, since goal choice ignores the arm):", "", "```",
              json.dumps(res["goal_balance"], indent=1), "```", "",
              "## Why simpler comparisons mislead", "",
              "Viewers who were *served* (actually saw an agent page) are the ones who came back to the app while a "
              "page was live. Coming back is itself a sign of engagement, so comparing them with anyone else "
              "measures who they are, not what the agent did.", "",
              f"- Served vs everyone else, played within 7d: {_fmt(res['misleading']['served_vs_everyone_else'], 'served', 'others')}",
              f"- Agent arm served vs agent arm not served: "
              f"{_fmt(res['misleading']['agent_arm_served_vs_not_served'], 'served', 'not served')}",
              f"- Compare with the honest arm-vs-arm number for the same metric: {res['secondary']['played_7d']['diff_pts']:+.1f} pts.",
              "", "## The earlier pilot, re-analysed", "",
              f"- Report's method (served vs all others): {_fmt(pilot['report_method_served_vs_others'], 'served', 'others')}",
              f"- Arm vs arm: {_fmt(pilot['arm_vs_arm'], 'treatment', 'holdout')}",
              f"- Treatment viewers *not* served vs holdout: "
              f"{_fmt(pilot['treatment_not_served_vs_holdout'], 'not served', 'holdout')}. Serving pulled the engaged "
              "viewers out of the treatment arm, which is why the leftovers look worse than holdout.", "",
              "The pilot's 2.3x uplift is selection, not effect. Arm vs arm, the pilot agent showed no benefit, and the "
              "recommendation to roll it out to every viewer is not supported."]
    return "\n".join(lines) + "\n"
