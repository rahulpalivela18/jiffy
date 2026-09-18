"""The complete agent loop. Typed choices, observable state, bounded execution.

Ported from browser-use/jev-ultrafast (MIT) with two additions:
  * confidence gate: stop and ask when Jev is unsure
  * UPLOAD support via a configured local file (resume)
"""

import base64
import time
from pathlib import Path

from .browser import Browser, StalePage
from .log import get as _get_logger
from .model import action_space, choose, field_context, field_text
from .profile import retrieve
from .questions import MAX_STEPS

log = _get_logger("agent")


def _fmt_probs(probabilities, top=3):
    """Compact top-N probability string for logs, e.g. 'CLICK=0.95 TYPE_TEXT=0.05'."""
    if not probabilities:
        return "-"
    ranked = sorted(probabilities.items(), key=lambda item: item[1], reverse=True)[:top]
    return " ".join(f"{key}={value:.2f}" for key, value in ranked)


class Agent:
    def __init__(
        self,
        url,
        goals,
        *,
        record_dir=None,
        screenshots=False,
        resume=None,
        profile=None,
        skill=None,
        max_steps=MAX_STEPS,
        confidence_floor=0.0,
        verifier=None,
        browser=None,
    ):
        task = goals.strip() if isinstance(goals, str) else "\n".join(goals).strip()
        if not task:
            raise ValueError("Supply a task")
        plan = [task]
        self.pending_text = None
        self.resume = resume
        self.profile = profile or {}
        self.skill = skill
        self.max_steps = max_steps
        self.confidence_floor = confidence_floor
        self.verifier = verifier
        self.browser = browser or Browser(url)
        self.record_dir = Path(record_dir) if record_dir else None
        self.screenshots = screenshots or bool(record_dir)
        try:
            page = self.browser.observe(screenshot=self.screenshots)
        except Exception:
            self.browser.close()
            raise
        self.state = dict(
            browser=self.browser,
            goal="\n".join(plan),
            page=page,
            decision=None,
            history=[],
            status="ready",
            plan=plan,
            plan_index=0,
            decisions=[],
            text_calls=[],
            elapsed_ms=0,
            started_at=None,
            record=bool(self.record_dir),
            needs_user=None,
        )
        if self.record_dir:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            (self.record_dir / "000000.jpg").write_bytes(
                base64.b64decode(page["screenshot"])
            )

    def snapshot(self):
        return {
            **{k: v for k, v in self.state.items() if k != "browser"},
            "elements": action_space(self.state["page"]["actions"])[0],
        }

    def _stop(self, status, reason):
        self.state["status"] = status
        self.state["needs_user"] = reason
        self.state["elapsed_ms"] = round(
            (time.perf_counter() - self.state["started_at"]) * 1000
        ) if self.state["started_at"] else 0
        log.warning("stopping: %s (%s)", status, reason)
        return self.snapshot()

    def command(self, name, body=None):
        body = body or {}
        state = self.state
        if name == "tick":
            try:
                self.command("predict", {})
                if state["status"] in {"needs_user", "done", "blocked"}:
                    return self.snapshot()
                return self.command(
                    "act", {"fingerprint": state["page"]["fingerprint"]}
                )
            except StalePage:
                log.debug("page changed before acting; re-observing")
                state["decision"] = None
                state["status"] = "ready"
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
                state["elapsed_ms"] = round(
                    (time.perf_counter() - state["started_at"]) * 1000
                )
                return self.snapshot()
        elif name == "predict":
            if not state["browser"]:
                raise ValueError("Start a run first")
            if state["started_at"] is None:
                state["started_at"] = time.perf_counter()
            if not state["browser"].fresh(state["page"]):
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
            state["decision"] = None
            if state["status"] in {"done", "blocked"}:
                raise ValueError("This run has stopped. Start a fresh run.")
            if len(state["decisions"]) >= self.max_steps * 2:
                raise ValueError("Reached the run's model-call budget")
            state["decision"] = choose(
                state["page"], state["goal"], state["history"], skill=self.skill
            )
            state["decisions"].append(
                {
                    **state["decision"],
                    "fingerprint": state["page"]["fingerprint"],
                    "elapsed_ms": round(
                        (time.perf_counter() - state["started_at"]) * 1000
                    ),
                }
            )
            if (
                self.confidence_floor
                and state["decision"]["confidence"] < self.confidence_floor
            ):
                return self._stop(
                    "needs_user",
                    f"Low confidence ({state['decision']['confidence']:.2f}) on "
                    f"{state['decision']['operation']} at "
                    f"{state['page']['url'][:60]!r}. Review before continuing.",
                )
            state["status"] = "predicted"
        elif name == "act":
            decision, page = state["decision"], state["page"]
            if not decision or body.get("fingerprint") != page["fingerprint"]:
                raise ValueError("Observe and choose before acting")
            # Consume once, before any mutation or model call. A retry cannot double-click.
            state["decision"] = None
            selected = decision["choice"]
            if selected in {"DONE", "BLOCKED"}:
                if not state["browser"].fresh(page):
                    state["status"] = "ready"
                    raise StalePage("Page changed since the decision. Choose again.")
                if selected == "DONE" and self.verifier and not self.verifier(state):
                    return self._stop(
                        "needs_user",
                        "Jev chose DONE but independent verification failed.",
                    )
                state["status"] = "done" if selected == "DONE" else "blocked"
                state["plan_index"] = int(selected == "DONE")
                state["elapsed_ms"] = round(
                    (time.perf_counter() - state["started_at"]) * 1000
                )
                log.info(
                    "terminal decision: %s (conf %.2f) op_p=%s | url=%s",
                    selected,
                    decision["confidence"],
                    _fmt_probs(decision.get("operation_probabilities")),
                    page["url"][:70],
                )
                log.info(
                    "finished: %s after %d steps in %dms",
                    state["status"],
                    len(state["history"]),
                    state["elapsed_ms"],
                )
                return self.snapshot()
            action = next(a for a in page["actions"] if a["id"] == selected)
            if len(state["history"]) >= self.max_steps:
                return self._stop(
                    "blocked", f"Stopped at the {self.max_steps}-action budget"
                )
            if action["kind"] == "upload" and not self.resume:
                return self._stop(
                    "needs_user",
                    f"Needs a file for '{action['label']}'. Pass --resume /path/to.pdf.",
                )
            text, helper = None, None
            if action["kind"] == "fill":
                if not state["browser"].fresh(page):
                    raise StalePage("Page changed before text generation. Choose again.")
                facts = (
                    retrieve(
                        self.profile,
                        f"{action['label']} {action.get('value', '')} "
                        f"{page['title']} {page['text'][:2000]}",
                    )
                    if self.profile
                    else {}
                )
                context = field_context(
                    state["goal"], action, page, state["history"], facts=facts
                )
                if self.pending_text and self.pending_text[0] == context:
                    _, text, helper = self.pending_text
                else:
                    text, helper = field_text(context)
                    self.pending_text = (context, text, helper)
                    state["text_calls"].append(
                        {**helper, "field": action["label"], "value": text}
                    )
            # Browser.act checks freshness immediately before input, including after text generation.
            state["browser"].act(
                action,
                page,
                text=text,
                file_path=self.resume if action["kind"] == "upload" else None,
            )
            self.pending_text = None
            state["elapsed_ms"] = round(
                (time.perf_counter() - state["started_at"]) * 1000
            )
            # Record execution before observing. A stale post-action observation must not erase the action.
            state["history"].append(
                {
                    "step": len(state["history"]) + 1,
                    "action": action["label"],
                    "kind": action["kind"],
                    "choice": selected,
                    "probability": decision["probabilities"].get(selected),
                    "confidence": decision["confidence"],
                    "latency_ms": decision["latency_ms"],
                    "text": text,
                    "text_helper": helper["model"] if helper else None,
                    "text_latency_ms": helper["latency_ms"] if helper else 0,
                    "operation": decision["operation"],
                    "target": decision["target"],
                    "operation_probabilities": decision.get("operation_probabilities", {}),
                    "target_probabilities": decision.get("target_probabilities", {}),
                    "target_confidence": decision.get("target_confidence"),
                    "page_changed": None,
                    "url": page["url"],
                    "usage": decision["usage"],
                    "executed_ms": round(
                        (time.perf_counter() - state["started_at"]) * 1000
                    ),
                    "elapsed_ms": state["elapsed_ms"],
                }
            )
            state["page"] = state["browser"].observe(screenshot=self.screenshots)
            state["elapsed_ms"] = round(
                (time.perf_counter() - state["started_at"]) * 1000
            )
            state["history"][-1].update(
                page_changed=state["page"]["fingerprint"] != page["fingerprint"],
                url=state["page"]["url"],
                elapsed_ms=state["elapsed_ms"],
            )
            record = state["history"][-1]
            log.info(
                "step %d: %s %s -> %s | op_p=%s | target_p=%s | conf=%.2f | %dms%s",
                record["step"],
                record["operation"],
                record["kind"],
                record["action"][:60],
                _fmt_probs(record.get("operation_probabilities")),
                _fmt_probs(record.get("target_probabilities")),
                record["confidence"],
                record["latency_ms"],
                f", text {record['text']!r}" if record.get("text") else "",
            )
            if record["page_changed"] is False and record["kind"] != "wait":
                log.debug("page did not change after step %d", record["step"])
            if state["record"]:
                (self.record_dir / f"{state['elapsed_ms']:06d}.jpg").write_bytes(
                    base64.b64decode(state["page"]["screenshot"])
                )
            repeated = state["history"][-3:]
            state["status"] = (
                "blocked"
                if len(repeated) == 3
                and all(h["page_changed"] is False and h["kind"] != "wait" for h in repeated)
                else "ready"
            )
        else:
            raise ValueError("Unknown command")
        return self.snapshot()

    def run(self):
        while self.state["status"] not in {"done", "blocked", "needs_user"}:
            yield self.command("tick")

    def close(self):
        self.browser.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
