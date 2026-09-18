"""Persistent CDP connection daemon.

Chrome 136+/144+ shows an "Allow remote debugging?" dialog for every new CDP
connection on the default profile. browser-harness avoids the repeated prompts
by holding one long-lived connection in a background daemon. This does the same
for jiffy:

    first run  -> daemon connects once, you click Allow once
    later runs -> reuse the same connection, no dialog

Endpoints (localhost only):
    GET  /health  -> {"ready": bool, "endpoint": str, "error": str|null}
    POST /run     -> run one goal and return the final agent state
"""

import json
import os
import signal
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from dotenv import load_dotenv

from .agent import Agent
from .browser import (
    Browser,
    connect_cdp,
    launch_dedicated_context,
)
from .log import get as get_logger
from .log import setup as setup_logging
from .questions import MAX_STEPS
from .skills import skill_for_url, skill_from_path

load_dotenv()

log = get_logger("daemon")

PORT = int(os.getenv("JIFFY_DAEMON_PORT", "8765"))
HOST = "127.0.0.1"

STATE = {
    "ready": False,
    "endpoint": None,
    "error": None,
}
SESSION = {"agent": None}
LOCK = threading.Lock()
PW = None
BROWSER = None
CONTEXT = None


def _connected():
    """True only if the browser is actually usable right now."""
    if not STATE["ready"]:
        return False
    if CONTEXT is not None:
        try:
            _ = CONTEXT.pages
            return True
        except Exception:  # noqa: BLE001
            return False
    if BROWSER is None:
        return False
    try:
        session = BROWSER.new_browser_cdp_session()
        session.send("Browser.getVersion")
        return True
    except Exception:  # noqa: BLE001 - any failure means disconnected
        return False


def _connect_once():
    global PW, BROWSER, CONTEXT
    if _connected():
        return
    if PW is not None:
        try:
            PW.stop()
        except Exception:  # noqa: BLE001
            pass
        PW, BROWSER, CONTEXT = None, None, None
    cdp = os.getenv("JIFFY_CDP_URL")
    try:
        if cdp:
            STATE["endpoint"] = cdp
            log.info("attaching over CDP to %s", cdp)
            PW, BROWSER = connect_cdp(cdp)
        else:
            log.info("launching the dedicated jiffy Chrome")
            PW, CONTEXT = launch_dedicated_context()
            STATE["endpoint"] = "dedicated"
    except Exception as exc:  # noqa: BLE001 - report via /health
        STATE["ready"] = False
        STATE["error"] = str(exc)
        log.error("browser start failed: %s", exc)
        return
    STATE["ready"] = True
    STATE["error"] = None
    log.info("connected; daemon ready on %s:%d", HOST, PORT)


def _close_session():
    agent = SESSION.get("agent")
    if agent is not None:
        try:
            agent.close()
        except Exception:  # noqa: BLE001
            pass
    SESSION["agent"] = None


def _drive(agent):
    """Run ticks until the agent stops for a reason the caller must handle."""
    while agent.state["status"] not in {"done", "blocked", "needs_user"}:
        agent.command("tick")


def _open_browser(url, *, current_tab=False, tab_match=None):
    if CONTEXT is not None:
        return Browser.from_context(
            CONTEXT, url, current_tab=current_tab, tab_match=tab_match
        )
    return Browser.from_cdp(BROWSER, url, current_tab=current_tab, tab_match=tab_match)


def _run_once(body):
    url = body.get("url") or "https://www.google.com"
    goal = (body.get("goal") or "").strip()
    if not goal:
        raise ValueError("missing goal")
    skill = skill_from_path(body.get("skill")) if body.get("skill") else skill_for_url(url)
    if skill:
        log.info("using site skill for %s", url[:60])
    with LOCK:
        _close_session()
        browser = _open_browser(
            url,
            current_tab=body.get("current_tab", False),
            tab_match=body.get("tab_match"),
        )
        agent = Agent(
            url,
            goal,
            browser=browser,
            resume=body.get("resume"),
            profile=body.get("profile") or {},
            skill=skill,
            max_steps=int(body.get("max_steps") or MAX_STEPS),
            confidence_floor=float(body.get("confidence_floor") or 0.0),
        )
        SESSION["agent"] = agent
        _drive(agent)
        return agent.snapshot()


def _resume(body):
    """Continue a paused run after the user answers.

    action=continue -> keep going with the confidence gate relaxed.
    action=guidance -> append the user's text to the goal, then continue.
    """
    agent = SESSION.get("agent")
    if agent is None:
        raise RuntimeError("no paused run to resume")
    with LOCK:
        action = body.get("action", "continue")
        if action == "guidance" and (body.get("text") or "").strip():
            agent.state["goal"] = (
                agent.state["goal"] + "\nUser guidance: " + body["text"].strip()
            )
        agent.confidence_floor = 0.0
        if agent.state["status"] == "needs_user":
            agent.state["status"] = "ready"
            agent.state["needs_user"] = None
        _drive(agent)
        return agent.snapshot()


def _observe(body):
    """Debug helper: return what the agent sees, without asking Jev anything."""
    if not _connected():
        _connect_once()
    if not _connected():
        raise RuntimeError(STATE["error"] or "daemon is not connected")
    url = body.get("url")
    with LOCK:
        browser = _open_browser(url or "about:blank", current_tab=not url)
        try:
            page = browser.observe(screenshot=False)
            webdriver = browser.evaluate("navigator.webdriver")
        finally:
            browser.close()
    return {
        "url": page["url"],
        "title": page["title"],
        "text": page["text"],
        "webdriver": webdriver,
        "actions": [
            {
                "id": a.get("id"),
                "kind": a.get("kind"),
                "role": a.get("role"),
                "label": a.get("label"),
            }
            for a in page["actions"]
        ],
    }


def _open(body):
    """Open a page in the shared browser and leave it there (used for login)."""
    if not _connected():
        _connect_once()
    if not _connected():
        raise RuntimeError(STATE["error"] or "daemon is not connected")
    url = body.get("url") or "https://www.google.com"
    with LOCK:
        browser = _open_browser(url, current_tab=False)
    return {"url": browser.page.url}


def _run(body):
    for attempt in (1, 2):
        if not _connected():
            _connect_once()
        if not _connected():
            raise RuntimeError(STATE["error"] or "daemon is not connected")
        try:
            return _run_once(body)
        except Exception as exc:  # noqa: BLE001 - retry once on a dropped connection
            message = str(exc).lower()
            dropped = (
                "closed" in message
                or "disconnected" in message
                or "target page" in message
                or not _connected()
            )
            if attempt == 1 and dropped:
                log.warning("browser connection dropped during run; reconnecting")
                STATE["ready"] = False
                continue
            raise
    raise RuntimeError("daemon is not connected")


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            if STATE["ready"] and not _connected():
                STATE["ready"] = False
                STATE["error"] = "browser disconnected"
            return self._send(200, STATE)
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/shutdown":
            _close_session()
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return self._send(200, {"stopping": True})
        if self.path == "/connect":
            _connect_once()
            return self._send(200, STATE)
        if self.path not in ("/run", "/resume", "/observe", "/open"):
            return self._send(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"error": "invalid JSON"})
        try:
            if self.path == "/run":
                result = _run(body)
            elif self.path == "/resume":
                result = _resume(body)
            elif self.path == "/open":
                result = _open(body)
            else:
                result = _observe(body)
            return self._send(200, result)
        except Exception as exc:  # noqa: BLE001 - report to caller
            log.error("%s failed: %s", self.path, exc)
            return self._send(500, {"error": str(exc)})

    def log_message(self, *_args):
        pass


def main():
    setup_logging(os.getenv("JIFFY_LOG", "normal"))
    # Bind the port FIRST so a second launch can never race us and take the
    # browser down. The browser is connected lazily on the first request.
    try:
        server = HTTPServer((HOST, PORT), Handler)
    except OSError as exc:
        print(f"daemon already running on {HOST}:{PORT} ({exc})", flush=True)
        return

    def stop(*_args):
        _close_session()
        server.shutdown()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(f"jiffy daemon on http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
