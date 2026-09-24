"""Compile a raw request into a structured plan, and track milestones.

The plan gives Jev the same contract a human would need: ordered milestones,
the literal strings to use, constraints, what "done" looks like, and what not to
do. It is sent with every decision so the agent stops re-guessing intent.
"""

from .model import _text_json
from .questions import PLAN


def compile_plan(goal, url=None):
    """Return a structured plan dict, or None if planning failed."""
    data = _text_json(PLAN, {"goal": goal, "start_url": url or ""}, max_tokens=900)
    if not isinstance(data, dict):
        return None
    milestones = [
        str(m).strip() for m in (data.get("milestones") or []) if str(m).strip()
    ]
    if not milestones:
        return None

    def strings(key):
        return [str(v).strip() for v in (data.get(key) or []) if str(v).strip()]

    exact = data.get("exact_values")
    start = data.get("start_url")
    return {
        "start_url": start if isinstance(start, str) and start.startswith("http") else None,
        "milestones": milestones,
        "exact_values": exact if isinstance(exact, dict) else {},
        "constraints": strings("constraints"),
        "done_when": strings("done_when"),
        "never": strings("never"),
    }


def plan_state(plan, index):
    """Render the plan with milestone statuses for the model."""
    milestones = plan.get("milestones") or []
    return {
        "milestones": [
            {
                "text": text,
                "status": "done" if i < index else "current" if i == index else "pending",
            }
            for i, text in enumerate(milestones)
        ],
        "current": milestones[index] if 0 <= index < len(milestones) else None,
        "exact_values": plan.get("exact_values") or {},
        "constraints": plan.get("constraints") or [],
        "done_when": plan.get("done_when") or [],
        "never": plan.get("never") or [],
    }


def plan_summary(plan):
    lines = ["Plan:"]
    for i, text in enumerate(plan.get("milestones") or [], 1):
        lines.append(f"  {i}. {text}")
    exact = plan.get("exact_values") or {}
    if exact:
        lines.append("Exact values (use verbatim):")
        for key, value in exact.items():
            lines.append(f"  {key}: {value!r}")
    for key, label in (("constraints", "Constraints"), ("done_when", "Done when"), ("never", "Never")):
        values = plan.get(key) or []
        if values:
            lines.append(f"{label}:")
            for value in values:
                lines.append(f"  - {value}")
    return "\n".join(lines)
