"""Model provider abstraction, call budget, and the mock provider used in replay.

The model is an advisor only. It is asked one narrow question: "of these rule-eligible goals, which fits this
viewer best?". Its answer is parsed strictly, and anything outside the eligible list is rejected by the caller.
Generated text is never evidence and never approval.

The shipped provider is deterministic so every evaluator run exercises the same decision and fault paths. A
production provider can implement the same `advise()` signature and remains subject to the same validation.
"""
import json


class ModelError(Exception):
    """Model unavailable, timed out, or returned something unusable. The caller falls back to rules."""


class BudgetExceeded(Exception):
    pass


class Budget:
    """Per-decision call budget (brief: configurable, default 8 model calls and 20 tool calls)."""

    def __init__(self, max_model_calls=8, max_tool_calls=20):
        self.max_model, self.max_tool = max_model_calls, max_tool_calls
        self.model_calls = self.tool_calls = 0
        self.waited_s = 0.0  # simulated time spent waiting on dependencies (added to measured latency)

    def model(self):
        if self.model_calls >= self.max_model:
            raise BudgetExceeded(f"model call budget {self.max_model} spent")
        self.model_calls += 1

    def tool(self):
        if self.tool_calls >= self.max_tool:
            raise BudgetExceeded(f"tool call budget {self.max_tool} spent")
        self.tool_calls += 1

    def summary(self):
        return {"model_calls": self.model_calls, "tool_calls": self.tool_calls,
                "simulated_wait_s": round(self.waited_s, 3)}


def parse_advice(raw):
    """Strict schema check. Anything malformed is a ModelError, never a partial answer."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ModelError(f"malformed model output: {error.msg}") from None
    if not isinstance(raw, dict) or not isinstance(raw.get("goal_id"), (str, type(None))):
        raise ModelError("model output missing goal_id")
    conf = raw.get("confidence")
    if not isinstance(conf, (int, float)) or not 0 <= conf <= 1:
        raise ModelError("model output confidence missing or out of range")
    return {"goal_id": raw["goal_id"], "confidence": float(conf), "reason": str(raw.get("reason", ""))[:300],
            "eligible_claim": raw.get("eligible")}


class MockProvider:
    """Deterministic stand-in for a hosted model.

    Normal answer: the first rule-eligible goal. Reproduces `replay/model_faults.jsonl` exactly, and honours
    dependency outages through `health(dependency, now) -> 'ok' | 'unavailable' | 'degraded'`.
    """
    NORMAL_LATENCY_S = 0.8

    def __init__(self, faults=(), health=lambda dep, now: "ok"):
        self.faults = {fault["trigger_event_id"]: fault for fault in faults}
        self.health = health

    def advise(self, request, trigger_event_id, now, budget, timeout_s):
        budget.model()
        if self.health("model_provider", now) == "unavailable":
            budget.waited_s += 0.2
            raise ModelError("model provider unavailable (HTTP 503)")
        fault = self.faults.get(trigger_event_id)
        if fault and fault["fault"] == "timeout":
            budget.waited_s += min(fault["seconds"], timeout_s)
            raise ModelError(f"model call timed out after {min(fault['seconds'], timeout_s)}s")
        budget.waited_s += self.NORMAL_LATENCY_S
        if fault:
            return parse_advice(fault["model_output"])
        eligible = request["eligible_goals"]
        return parse_advice({"goal_id": eligible[0] if eligible else None, "eligible": bool(eligible),
                             "confidence": 0.6, "reason": "mock: first rule-eligible goal"})
