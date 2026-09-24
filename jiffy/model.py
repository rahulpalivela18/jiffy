"""TypeSafe's Jev makes choices; a small OpenAI-compatible model writes field values.

Ported from browser-use/jev-ultrafast (MIT) and adapted:
  * adds UPLOAD to the action space
  * TEXT_MODEL_BASE_URL supports OpenRouter / DeepSeek / Kimi / any OpenAI-compatible endpoint
"""

import json
import math
import os
import re
import time
from urllib.parse import urlparse

import httpx

from .log import get as _get_logger
from .questions import NEXT_ACTION, START_URL, TARGET, TEXT_VALUE

log = _get_logger("model")

CLIENT = httpx.Client(timeout=25)

TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"


def post_json(url, key, body, extra_headers=None):
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    for attempt in range(3):
        try:
            response = CLIENT.post(url, json=body, headers=headers)
        except httpx.HTTPError:
            raise RuntimeError("Model connection failed; no action executed.") from None
        if response.status_code in {429, 529, 503} and attempt < 2:
            time.sleep(0.5 * 2**attempt)
            continue
        if response.is_error:
            raise RuntimeError(
                f"Model provider returned HTTP {response.status_code}; no action executed."
            )
        return response.json()
    raise RuntimeError("Model unavailable")


def validate_choice(answer, ids):
    try:
        probabilities = answer["probabilities"]
        numbers = [*probabilities.values(), answer["confidence"]]
        valid = (
            answer["choice"] in ids
            and set(probabilities) == set(ids)
            and all(
                type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1 for n in numbers
            )
            and abs(sum(probabilities.values()) - 1) < 0.02
            and probabilities[answer["choice"]] >= max(probabilities.values()) - 1e-6
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("Invalid TypeSafe response; no action executed.")
    return answer


def action_space(actions):
    """One index per observed element; each operation has its own valid target choices."""
    elements, indices, targets, controls = [], {}, {}, {}
    operations = {"click": "CLICK", "fill": "TYPE_TEXT", "select": "SELECT", "upload": "UPLOAD"}
    for action in actions:
        kind = action["kind"]
        if kind not in operations:
            controls[action["id"].upper()] = action
            continue
        node = action["node"]
        if node not in indices:
            index = str(len(elements) + 1)
            indices[node] = index
            element = {
                k: action[k]
                for k in ("role", "value", "checked", "selected", "expanded")
                if k in action
            }
            element.update(index=index, label=action["label"].split(" → ")[0], operations=[])
            if kind == "select":
                element["value"] = action.get("current_value", "")
                element["options"] = []
            elements.append(element)
        index = indices[node]
        operation = operations[kind]
        group = targets.setdefault(operation, {})
        element = elements[int(index) - 1]
        if operation not in element["operations"]:
            element["operations"].append(operation)
        target = index
        if kind == "select":
            target = f"{index}:{len(element['options']) + 1}"
            element["options"].append(
                {"index": target, "label": action["label"], "value": action["value"]}
            )
        group[target] = action
    return elements, targets, controls


def _instructions(goal, skill, *, rules, operation=None, plan=None, milestone_index=0):
    instructions = {"goal": goal, "rules": rules}
    if operation:
        instructions["operation"] = operation
    if skill:
        instructions["site_skill"] = skill
    if plan:
        from .plan import plan_state

        state = plan_state(plan, milestone_index)
        instructions["current_milestone"] = state["current"]
        instructions["exact_values"] = state["exact_values"]
        instructions["constraints"] = state["constraints"]
        instructions["never"] = state["never"]
    return instructions


def choose(state, goal, history, model=None, skill=None, plan=None, milestone_index=0):
    elements, targets, controls = action_space(state["actions"])
    labels = {
        "CLICK": "Click an element, button, menu option, autocomplete suggestion, or calendar day.",
        "TYPE_TEXT": "Enter or replace text in an editable field. A small LLM will supply the value from the goal.",
        "SELECT": "Select an observed dropdown value.",
        "UPLOAD": "Attach a local file to this file input (used for resumes and documents).",
    }
    operations = {key: labels[key] for key in targets}
    operations.update({key: value["label"] for key, value in controls.items()})
    operations.update(
        DONE="Every requirement is visibly satisfied.",
        BLOCKED="No supported operation can progress.",
    )
    questions = {
        "operation": {
            "type": "choice",
            "criteria": operations,
            "instructions": _instructions(
                goal, skill, rules=NEXT_ACTION, plan=plan, milestone_index=milestone_index
            ),
        }
    }
    for operation, candidates in targets.items():
        questions[operation.lower() + "_target"] = {
            "type": "choice",
            "criteria": {
                index: {
                    "element": f"[{index}] {a['label']}",
                    "current_value": a.get("current_value", a.get("value", "")),
                    **{k: a[k] for k in ("role", "checked", "selected", "expanded") if k in a},
                }
                for index, a in candidates.items()
            },
            "instructions": _instructions(
                goal,
                skill,
                operation=operation,
                rules=[NEXT_ACTION, TARGET],
                plan=plan,
                milestone_index=milestone_index,
            ),
        }
    plan_block = None
    if plan:
        from .plan import plan_state

        plan_block = plan_state(plan, milestone_index)
    body = {
        "model": model or os.environ.get("TYPESAFE_MODEL", "jev-latest"),
        "state": {
            "page": {k: state[k] for k in ("url", "title", "text")},
            "elements": elements,
            "plan": plan_block,
            "recent_actions": [
                {k: h.get(k) for k in ("action", "kind", "text", "page_changed")}
                for h in history[-10:]
            ],
        },
        "questions": questions,
    }
    started = time.perf_counter()
    log.debug(
        "Jev request: %d elements, operations=%s, targets=%s",
        len(elements),
        sorted(operations),
        {k: len(v) for k, v in targets.items()},
    )
    result = post_json(TYPESAFE_URL, os.environ["TYPESAFE_API_KEY"], body)
    operation_answer = validate_choice(result["answers"].get("operation", {}), operations)
    operation = operation_answer["choice"]
    target = None
    target_answer = None
    probabilities = {}
    if operation in targets:
        # Unused target heads cannot cause an action. Validate the head selected by the operation.
        target_answer = validate_choice(
            result["answers"].get(operation.lower() + "_target", {}), targets[operation]
        )
        target = target_answer["choice"]
        choice = targets[operation][target]["id"]
        probabilities = {
            a["id"]: target_answer["probabilities"][index]
            for index, a in targets[operation].items()
        }
    else:
        choice = controls[operation]["id"] if operation in controls else operation
        probabilities[choice] = operation_answer["probabilities"][operation]
    log.debug(
        "Jev decision: op=%s target=%s confidence=%.3f latency=%dms usage=%s",
        operation,
        target,
        operation_answer["confidence"],
        round((time.perf_counter() - started) * 1000),
        result.get("usage", {}),
    )
    return {
        "choice": choice,
        "operation": operation,
        "target": target,
        "confidence": operation_answer["confidence"],
        "probabilities": probabilities,
        "operation_probabilities": operation_answer["probabilities"],
        "target_probabilities": target_answer["probabilities"] if target_answer else {},
        "target_confidence": target_answer["confidence"] if target_answer else None,
        "raw_answers": result["answers"],
        "model": result["model"],
        "usage": result.get("usage", {}),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "request": body,
    }


def field_context(goal, action, page, history, facts=None):
    return {
        "goal": goal,
        "field": {k: action.get(k) for k in ("label", "role", "value")},
        "page": {"title": page["title"], "text": page["text"][:6000]},
        "recent_actions": [{k: h.get(k) for k in ("action", "text")} for h in history[-6:]],
        "facts": facts or {},
    }


def _reasoning_payload(base):
    if os.environ.get("TEXT_MODEL_REASONING") == "none":
        return {"reasoning": {"enabled": False}}
    if "deepseek" in base:
        return {"thinking": {"type": "disabled"}}
    return {"reasoning": {"effort": "low"}}


def field_text(context):
    key = os.environ.get("TEXT_MODEL_API_KEY")
    if not key:
        raise ValueError(
            "TYPE_TEXT needs TEXT_MODEL_API_KEY; no text is hardcoded or guessed by the executor."
        )
    base = os.environ.get("TEXT_MODEL_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
    model = os.environ.get("TEXT_MODEL", "deepseek/deepseek-chat")
    headers = {}
    if "openrouter.ai" in base:
        headers = {
            "HTTP-Referer": "https://github.com/rahulpalivela18/jiffy",
            "X-Title": "jiffy",
        }
    started = time.perf_counter()
    body = {
        "model": model,
        "max_tokens": 1024,
        "response_format": {"type": "json_object"},
        **_reasoning_payload(base),
        "messages": [
            {"role": "system", "content": TEXT_VALUE},
            {"role": "user", "content": json.dumps(context)},
        ],
    }
    last_error = None
    for attempt in range(2):
        result = post_json(base + "/chat/completions", key, body, extra_headers=headers)
        try:
            value = _parse_text(result["choices"][0]["message"]["content"])
        except (ValueError, KeyError, TypeError) as exc:
            last_error = exc
            continue
        log.debug("text helper (%s) -> %r in %dms", model, value, round((time.perf_counter() - started) * 1000))
        return value, {
            "model": model,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "usage": result.get("usage", {}),
        }
    raise ValueError(
        "Text helper returned no valid field value; nothing typed."
    ) from last_error


def _parse_text(content):
    """Accept a strict {"text": "..."} object, tolerating stray prose or code fences."""
    try:
        output = json.loads(content)
    except (TypeError, ValueError):
        match = re.search(r"\{.*\}", content or "", re.DOTALL)
        if not match:
            raise ValueError("not JSON")
        output = json.loads(match.group())
    if set(output) != {"text"}:
        raise ValueError("unexpected keys")
    value = output["text"]
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise ValueError("empty or oversized value")
    return value


def _text_json(system_prompt, user_obj, max_tokens=256):
    """One small-model call returning a parsed JSON object, or None on failure."""
    key = os.environ.get("TEXT_MODEL_API_KEY")
    if not key:
        return None
    base = os.environ.get("TEXT_MODEL_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
    model = os.environ.get("TEXT_MODEL", "deepseek/deepseek-chat")
    headers = {}
    if "openrouter.ai" in base:
        headers = {
            "HTTP-Referer": "https://github.com/rahulpalivela18/jiffy",
            "X-Title": "jiffy",
        }
    try:
        result = post_json(
            base + "/chat/completions",
            key,
            {
                "model": model,
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
                **_reasoning_payload(base),
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(user_obj)},
                ],
            },
            extra_headers=headers,
        )
        return json.loads(result["choices"][0]["message"]["content"])
    except (ValueError, KeyError, TypeError, RuntimeError):
        return None


def milestone_check(milestone, page, history, threshold=0.6):
    """Ask Jev (Noul) whether a milestone is visibly complete."""
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key:
        return False
    body = {
        "model": os.environ.get("TYPESAFE_MODEL", "jev-latest"),
        "state": {
            "page": {k: page[k] for k in ("url", "title", "text")},
            "recent_actions": [
                {k: h.get(k) for k in ("action", "kind", "text", "page_changed")}
                for h in history[-6:]
            ],
        },
        "questions": {
            "complete": {
                "type": "noul",
                "instructions": {
                    "goal": f"Is this milestone visibly complete? {milestone}"
                },
            }
        },
    }
    try:
        result = post_json(TYPESAFE_URL, key, body)
        answer = result["answers"]["complete"]
    except (RuntimeError, KeyError, TypeError, ValueError):
        return False
    probability = answer.get("noul")
    if probability is None:
        probability = answer.get("probability")
    try:
        return float(probability) >= threshold
    except (TypeError, ValueError):
        return False


def start_url_for_goal(goal):
    """Ask the cheap model for the best starting URL, then validate it in code.

    This is the one string Jev cannot produce. The result is only used as a
    navigation target after scheme/host validation, never as code or selectors.
    """
    data = _text_json(START_URL, {"goal": goal})
    if not isinstance(data, dict):
        return None
    url = data.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or "." not in (parsed.hostname or ""):
        return None
    log.info("resolved start url: %s", url)
    return url
