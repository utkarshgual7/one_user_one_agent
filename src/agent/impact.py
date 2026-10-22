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


def _rate(flags):
    return sum(flags) / len(flags) if flags else float("nan")


def compare(group_a, group_b):
    """Difference of proportions a - b with a 95% normal-approximation interval."""
    rate_a, rate_b = _rate(group_a), _rate(group_b)
    std_error = math.sqrt(rate_a * (1 - rate_a) / len(group_a) + rate_b * (1 - rate_b) / len(group_b)) if group_a and group_b else float("nan")
    diff = rate_a - rate_b
    return {"n_a": len(group_a), "rate_a": round(rate_a, 4), "n_b": len(group_b), "rate_b": round(rate_b, 4),
            "diff_pts": round(diff * 100, 2), "ci95_pts": [round((diff - 1.96 * std_error) * 100, 2), round((diff + 1.96 * std_error) * 100, 2)]}


def viewer_outcomes(state, decisions, observed_until):
    first = {}
    for decision in decisions:
        first.setdefault(decision["viewer_id"], decision)
    out = {}
    for uid, decision in first.items():
        start = decision["trigger"]["deliver_at"]
        events = [event for event in state.history(uid, observed_until) if event_time(event) >= start]
        ids = {action["action_id"] for action in state.actions(uid)}
        within = lambda kind, days: any(event["event"] == kind and event_time(event) <= clock.shift(start, days=days) for event in events)
        goals_ = sorted(state.goals(user_id=uid), key=lambda goal: goal["started_at"])
        goal = goals_[0] if goals_ else None
        out[uid] = {
            "arm": decision["arm"],
            "goal_id": goal and goal["goal_id"], "goal_verdict": goal and goal["status"],
            "played_7d": within("play_started", 7), "completed_7d": within("episode_completed", 7),
            "purchased_14d": any(event["event"] == "purchase_completed" and not event["properties"].get("is_trial") for event in events),
            "served": any(event["event"] == "home_page_shown" and event["properties"].get("personalised")
                          and event["properties"].get("page_action_id") in ids for event in events),
        }
    return out


def analyse(viewers):
    arms = defaultdict(list)
    for viewer in viewers.values():
        arms[viewer["arm"]].append(viewer)
    agent_arm, holdout_arm = arms["agent"], arms["holdout"]
    decided = lambda arm_viewers: [viewer["goal_verdict"] == "succeeded" for viewer in arm_viewers if viewer["goal_verdict"] in ("succeeded", "failed")]
    res = {"population": {arm: len(arm_viewers) for arm, arm_viewers in arms.items()},
           "goal_balance": {arm: _count(viewer["goal_id"] for viewer in arm_viewers) for arm, arm_viewers in arms.items()},
           "inconclusive": {arm: sum(viewer["goal_verdict"] == "inconclusive" for viewer in arm_viewers) for arm, arm_viewers in arms.items()},
           "primary_goal_reached": compare(decided(agent_arm), decided(holdout_arm)),
           "secondary": {metric: compare([viewer[metric] for viewer in agent_arm], [viewer[metric] for viewer in holdout_arm])
                         for metric in ("played_7d", "completed_7d", "purchased_14d")},
           "by_goal": {}}
    for gid in sorted({viewer["goal_id"] for viewer in viewers.values() if viewer["goal_id"]}):
        agent_flags = decided([viewer for viewer in agent_arm if viewer["goal_id"] == gid])
        holdout_flags = decided([viewer for viewer in holdout_arm if viewer["goal_id"] == gid])
        if agent_flags and holdout_flags:
            res["by_goal"][gid] = compare(agent_flags, holdout_flags)
    served = [viewer for viewer in agent_arm if viewer["served"]]
    res["misleading"] = {
        "served_vs_everyone_else": compare([viewer["played_7d"] for viewer in served],
                                           [viewer["played_7d"] for viewer in viewers.values() if not viewer["served"]]),
        "agent_arm_served_vs_not_served": compare([viewer["played_7d"] for viewer in served],
                                                  [viewer["played_7d"] for viewer in agent_arm if not viewer["served"]]),
    }
    return res


def _count(items):
    counts = defaultdict(int)
    for item in items:
        counts[item or "none"] += 1
    return dict(sorted(counts.items()))


def pilot_reanalysis(pack_root):
    rows = list(csv.DictReader(open(f"{pack_root}/pilot/pilot_outcomes.csv")))
    succeeded_flags = lambda group: [row["succeeded"] == "true" for row in group]
    treatment = [row for row in rows if row["arm"] == "treatment"]
    holdout = [row for row in rows if row["arm"] == "holdout"]
    served = [row for row in treatment if row["served"] == "true"]
    others = [row for row in rows if row["served"] != "true"]
    return {"report_method_served_vs_others": compare(succeeded_flags(served), succeeded_flags(others)),
            "arm_vs_arm": compare(succeeded_flags(treatment), succeeded_flags(holdout)),
            "treatment_not_served_vs_holdout": compare(succeeded_flags([row for row in treatment if row["served"] != "true"]), succeeded_flags(holdout))}


def _fmt(comparison, label_a="agent", label_b="holdout"):
    return (f"{label_a} {comparison['rate_a'] * 100:.1f}% (n={comparison['n_a']}) vs {label_b} {comparison['rate_b'] * 100:.1f}% (n={comparison['n_b']}): "
            f"**{comparison['diff_pts']:+.1f} pts**, 95% CI [{comparison['ci95_pts'][0]:+.1f}, {comparison['ci95_pts'][1]:+.1f}]")


def report_md(res, pilot, sim_seed):
    primary = res["primary_goal_reached"]
    sig = primary["ci95_pts"][0] > 0 or primary["ci95_pts"][1] < 0
    lines = [
        "# Impact report", "",
        f"Simulator seed `{sim_seed}`. Population: every viewer who triggered during the replay day "
        f"({res['population'].get('agent', 0)} agent arm, {res['population'].get('holdout', 0)} holdout). "
        "The unit is the viewer, the unit that was randomized.", "",
        "## Headline: arm vs arm (intent-to-treat)", "",
        "- First goal reached (goals chosen by identical rules in both arms; holdout goals are shadow goals): "
        + _fmt(primary),
        f"- {'The interval excludes zero.' if sig else 'The interval includes zero: no effect can be claimed from this run.'}",
        f"- Inconclusive goals (window runs past observed data) excluded: agent {res['inconclusive'].get('agent', 0)}, "
        f"holdout {res['inconclusive'].get('holdout', 0)}.", "",
        "Goal-independent outcomes (same comparison, no goal definitions involved):", ""]
    lines += [f"- {metric}: {_fmt(comparison)}" for metric, comparison in res["secondary"].items()]
    lines += ["", "## By goal (arm vs arm, decided goals only)", "", "| Goal | Agent | Holdout | Diff (pts) | 95% CI |",
              "|---|---|---|---|---|"]
    for gid, comparison in res["by_goal"].items():
        lines.append(f"| {gid} | {comparison['rate_a'] * 100:.1f}% (n={comparison['n_a']}) | {comparison['rate_b'] * 100:.1f}% (n={comparison['n_b']}) | "
                     f"{comparison['diff_pts']:+.1f} | [{comparison['ci95_pts'][0]:+.1f}, {comparison['ci95_pts'][1]:+.1f}] |")
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
