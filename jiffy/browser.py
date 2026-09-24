"""Observed actions through a real Chrome page; one Playwright session, no per-step subprocess.

Ported from browser-use/jev-ultrafast (MIT). Same contract as the original:
  observe() -> typed state, fresh() -> staleness guard, act() -> validated input.
The difference is transport: Playwright instead of browser-harness CDP.
"""

import base64
import hashlib
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from .errors import StalePage
from .log import get as _get_logger

log = _get_logger("browser")

READ_STATE = Path(__file__).with_name("snapshot.js").read_text()
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"

AFTER_INPUT = """(action => new Promise(resolve => {
  const field = window.__jiffy?.nodes.get(action.node);
  const autocomplete = action.kind === 'fill' && field?.getAttribute('role') === 'combobox';
  let frames = 0, stopped = false;
  const finish = () => { stopped = true; resolve(); };
  setTimeout(finish, autocomplete ? 200 : 50);
  const ready = () => {
    if (stopped) return;
    const ids = (field?.getAttribute('aria-controls') || field?.getAttribute('aria-owns') || '')
      .split(/\\s+/).filter(Boolean);
    const roots = ids.length ? ids.map(id => document.getElementById(id)).filter(Boolean) : [document];
    const options = roots.flatMap(root => [...root.querySelectorAll('[role="option"]')]);
    if (++frames >= 2 && (!autocomplete || options.some(e => {
      const r = e.getBoundingClientRect();
      return r.width && r.height && r.bottom > 0 && r.top < innerHeight &&
        e.checkVisibility({checkOpacity:true, checkVisibilityCSS:true});
    }))) finish();
    else requestAnimationFrame(ready);
  };
  requestAnimationFrame(ready);
}))"""

HITTEST = """(action => {
  const e = window.__jiffy?.nodes.get(action.node);
  if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
      !e.checkVisibility({checkOpacity:true, checkVisibilityCSS:true})) return null;
  if (action.kind === 'fill' && (e.readOnly || e.getAttribute('aria-readonly') === 'true')) return null;
  const r = e.getBoundingClientRect(), x = r.x + r.width/2, y = r.y + r.height/2;
  if (!r.width || !r.height || x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) return null;
  if (!e.contains(document.elementFromPoint(x, y))) return null;
  return {x, y};
})"""

GUARD = """(node => { const c = window.__jiffy; return c ? [c.pageKey(), c.guard(c.nodes.get(node))] : null; })"""

# Where Chromium browsers write the port they are listening on for CDP.
# Mirrors browser-harness's profile scan (Chrome, Canary, Comet, Arc, Dia, Edge, Brave).
_CDP_PROFILE_DIRS = (
    "Library/Application Support/Google/Chrome",
    "Library/Application Support/Google/Chrome Canary",
    "Library/Application Support/Comet",
    "Library/Application Support/Arc/User Data",
    "Library/Application Support/Dia/User Data",
    "Library/Application Support/Microsoft Edge",
    "Library/Application Support/Microsoft Edge Beta",
    "Library/Application Support/BraveSoftware/Brave-Browser",
    "Library/Application Support/BraveSoftware/Brave-Origin",
    "Library/Application Support/Chromium",
    ".config/google-chrome",
    ".config/chromium",
    ".config/chromium-browser",
    ".config/microsoft-edge",
    ".config/BraveSoftware/Brave-Browser",
)


def _port_live(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        return True
    except OSError:
        return False


def _ws_path_for_port(port):
    """Look up the DevTools ws path Chrome wrote for a port, across profiles."""
    for rel in _CDP_PROFILE_DIRS:
        active = Path.home() / rel / "DevToolsActivePort"
        if not active.exists():
            continue
        try:
            lines = active.read_text(errors="replace").splitlines()
            if int(lines[0].strip()) == port and len(lines) > 1:
                return lines[1].strip()
        except (ValueError, IndexError, OSError):
            continue
    return ""


def _endpoint_for_port(port, ws_path=""):
    """Return the best CDP endpoint for a live port.

    Prefer the plain http endpoint: Playwright resolves it itself and is more
    reliable with Chrome's per-connection approval than a raw ws URL. Chrome
    147+ returns 404 on /json/* for the default profile, so fall back to the
    ws path Chrome wrote to DevToolsActivePort.
    """
    if not ws_path:
        ws_path = _ws_path_for_port(port)
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=1
        ) as response:
            json.loads(response.read())
        return f"http://127.0.0.1:{port}"
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and ws_path:
            return f"ws://127.0.0.1:{port}{ws_path}"
        return None
    except Exception:  # noqa: BLE001 - any failure means fall through
        return None


def discover_cdp_endpoint():
    """Find a running Chromium browser that has remote debugging enabled.

    Follows the same discovery order as browser-use/browser-harness:
    DevToolsActivePort per profile -> /json/version -> stored ws path ->
    well-known ports 9222/9223. Returns an http:// or ws:// endpoint, or None.
    """
    for rel in _CDP_PROFILE_DIRS:
        base = Path.home() / rel
        active = base / "DevToolsActivePort"
        if not active.exists():
            continue
        try:
            lines = active.read_text(errors="replace").splitlines()
            port = int(lines[0].strip())
            ws_path = lines[1].strip() if len(lines) > 1 else ""
        except (ValueError, IndexError, OSError):
            continue
        if not _port_live(port):
            continue
        return _endpoint_for_port(port, ws_path)
    for port in (9222, 9223):
        if not _port_live(port):
            continue
        endpoint = _endpoint_for_port(port)
        if endpoint:
            return endpoint
    return None


def open_remote_debugging_page():
    """Open chrome://inspect/#remote-debugging in the user's Chromium browser.

    Returns the app name that opened it, or None. Best effort only.
    """
    candidates = (
        "Google Chrome",
        "Google Chrome 2",
        "Google Chrome Beta",
        "Google Chrome Canary",
        "Chromium",
        "Brave Browser",
        "Microsoft Edge",
        "Arc",
        "Dia",
        "Comet",
    )
    for app in candidates:
        try:
            result = subprocess.run(
                ["open", "-a", app, "chrome://inspect/#remote-debugging"],
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return app
    return None


def wait_for_cdp(timeout=120, interval=0.5):
    """Poll for a remote-debugging endpoint (e.g. while the user ticks the box)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        endpoint = discover_cdp_endpoint()
        if endpoint:
            return endpoint
        time.sleep(interval)
    return None


def activate_chrome():
    """Bring a Chromium browser to the front so a real click can land on its dialog."""
    for app in (
        "Google Chrome 2",
        "Google Chrome",
        "Google Chrome Beta",
        "Chromium",
        "Brave Browser",
        "Microsoft Edge",
    ):
        try:
            result = subprocess.run(["open", "-a", app], capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return app
    return None


def start_allow_clicker(timeout=100):
    """Background thread that clicks Chrome's Allow sheet while a connect is live.

    Chrome 144+ keeps the sheet on screen only while the connection that raised
    it is alive, so this must run concurrently with connect.
    """
    stop = threading.Event()

    def click_loop():
        from .approve import accessibility_granted, approve_once

        if not accessibility_granted():
            log.warning(
                "Accessibility permission not granted; grant it to this terminal "
                "to auto-click Allow, or click it manually"
            )
            return
        # A real click needs the dialog visible, so bring Chrome forward once.
        activate_chrome()
        deadline = time.time() + timeout
        while not stop.is_set() and time.time() < deadline:
            result = approve_once()
            log.debug("approve attempt -> %s", result)
            if result == "ready":
                log.info("clicked Chrome's 'Allow remote debugging?' sheet")
                return
            time.sleep(0.4)

    threading.Thread(target=click_loop, daemon=True).start()
    return stop


def connect_cdp(endpoint, attempts=4, timeout=25000):
    """Connect over CDP, retrying while the user (or the clicker) allows it.

    Returns (playwright_instance, browser). The caller owns both.
    """
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    stop_clicking = start_allow_clicker()
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            browser = pw.chromium.connect_over_cdp(endpoint, timeout=timeout)
            stop_clicking.set()
            return pw, browser
        except PWTimeout as exc:
            last_error = exc
            log.warning(
                "Chrome has not approved the connection (attempt %d/%d). "
                "Click Allow in the Chrome dialog.",
                attempt,
                attempts,
            )
    stop_clicking.set()
    pw.stop()
    raise RuntimeError(
        "Chrome did not approve the CDP connection. Click Allow in Chrome, then retry."
    ) from last_error


def _port_from_endpoint(endpoint):
    try:
        from urllib.parse import urlparse

        return urlparse(endpoint).port
    except (TypeError, ValueError):
        return None


def ensure_browser_endpoint(port=9333):
    """Return a live CDP endpoint, launching the dedicated Chrome if needed.

    Order: JIFFY_CDP_URL if live -> relaunch dedicated on that port -> discover
    a running Chromium -> launch the dedicated Chrome on `port`.
    """
    configured = os.getenv("JIFFY_CDP_URL")
    if configured:
        configured_port = _port_from_endpoint(configured)
        if configured_port and _port_live(configured_port):
            return configured
        if configured_port:
            try:
                return launch_automation_chrome(port=configured_port)
            except Exception as exc:  # noqa: BLE001 - fall through to discovery
                log.debug("could not relaunch on port %s: %s", configured_port, exc)
    endpoint = discover_cdp_endpoint()
    if endpoint:
        return endpoint
    return launch_automation_chrome(port=port)


def launch_dedicated_context(profile_dir=None, headless=False, channel=None):
    """Launch the jiffy Chrome with Playwright's persistent context.

    No CDP: Playwright owns the browser directly, so there is no remote-debugging
    dialog, no context-management errors, and no reconnect dance. The profile
    persists, so logins survive.
    """
    from .profile_sync import sync_into

    profile_dir = Path(profile_dir or (Path.home() / ".jiffy" / "chrome-profile"))
    profile_dir.mkdir(parents=True, exist_ok=True)
    # Sync cookies/theme only the first time. Re-syncing on every launch would
    # overwrite logins the user did inside the jiffy window.
    if not (profile_dir / "Default" / "Cookies").exists():
        try:
            sync_into(profile_dir)
        except Exception as exc:  # noqa: BLE001 - sync is best effort
            log.warning("profile sync skipped: %s", exc)
    pw = sync_playwright().start()
    kwargs = {
        "headless": headless,
        "viewport": {"width": 1120, "height": 780},
        "ignore_default_args": ["--enable-automation"],
        "args": [
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
        ],
    }
    if channel:
        kwargs["channel"] = channel
    else:
        executable = chrome_executable()
        if executable:
            kwargs["executable_path"] = executable
    try:
        context = pw.chromium.launch_persistent_context(str(profile_dir), **kwargs)
    except Exception:  # noqa: BLE001 - fall back to bundled browser
        kwargs.pop("channel", None)
        kwargs.pop("executable_path", None)
        context = pw.chromium.launch_persistent_context(str(profile_dir), **kwargs)
    log.info("launched dedicated Chrome with profile %s", profile_dir)
    return pw, context


def chrome_executable():
    """Find a Chrome/Chromium-family executable on this machine."""
    mac = (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Google Chrome 2.app/Contents/MacOS/Google Chrome",
        "/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta",
        "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    )
    for path in mac:
        if Path(path).exists():
            return path
    for name in ("google-chrome", "chromium", "chromium-browser", "brave-browser", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            return found
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            return pw.chromium.executable_path
    except Exception:  # noqa: BLE001
        return None


def launch_automation_chrome(profile_dir=None, port=9333, url="about:blank", sync=True):
    """Launch a dedicated Chrome with remote debugging on a non-default profile.

    This is the friction-free path: a non-default user-data-dir avoids Chrome's
    default-profile lockdown and its per-connection "Allow remote debugging"
    dialog. Log in once in this profile and it persists.

    With sync=True, cookies and theme are copied from the real Chrome profile
    first, so the automation browser starts already signed in.

    Uses its own port so it never collides with a Chrome the user already runs.
    """
    if _port_live(port):
        endpoint = _endpoint_for_port(port)
        if endpoint:
            log.info("reusing existing automation Chrome on port %d", port)
            return endpoint
    executable = chrome_executable()
    if not executable:
        raise RuntimeError("No Chrome/Chromium executable found on this machine.")
    profile_dir = Path(profile_dir or (Path.home() / ".jiffy" / "chrome-profile"))
    profile_dir.mkdir(parents=True, exist_ok=True)
    if sync:
        try:
            from .profile_sync import sync_into

            sync_into(profile_dir)
        except Exception as exc:  # noqa: BLE001 - sync is best effort
            log.warning("profile sync skipped: %s", exc)
    log.info("launching dedicated Chrome on port %d with profile %s", port, profile_dir)
    subprocess.Popen(
        [
            executable,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.time() + 25
    while time.time() < deadline:
        if _port_live(port):
            endpoint = _endpoint_for_port(port)
            if endpoint:
                return endpoint
        time.sleep(0.3)
    raise RuntimeError(
        f"Chrome did not open remote debugging on port {port}. "
        "Close other automation Chrome windows and retry."
    )


def _local_state_toggle():
    """Read chrome://inspect's 'Allow remote debugging' toggle from Local State."""
    for rel in _CDP_PROFILE_DIRS:
        state_path = Path.home() / rel / "Local State"
        if not state_path.exists():
            continue
        try:
            state = json.loads(state_path.read_text(errors="replace"))
        except (OSError, ValueError):
            continue
        value = (
            ((state.get("devtools") or {}).get("remote_debugging") or {}).get(
                "user-enabled"
            )
        )
        if value is not None:
            return value
    return None


def check_endpoint(endpoint, timeout=8000):
    """True when a CDP connection to endpoint succeeds right now."""
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(endpoint, timeout=timeout)
            browser.close()
            return True
        except (PWTimeout, Exception):  # noqa: BLE001 - any failure means not ready
            return False


def doctor():
    """Return a dict describing whether attaching can work, for diagnostics."""
    return {
        "endpoint": discover_cdp_endpoint(),
        "chrome_executable": chrome_executable(),
        "remote_debugging_toggle": _local_state_toggle(),
        "automation_profile": str(Path.home() / ".jiffy" / "chrome-profile"),
        "automation_profile_exists": (Path.home() / ".jiffy" / "chrome-profile").exists(),
    }


def action_delay():
    """Seconds to wait between actions so pages can settle. JIFFY_ACTION_DELAY."""
    try:
        return max(0.0, float(os.getenv("JIFFY_ACTION_DELAY", "1.5")))
    except (TypeError, ValueError):
        return 1.5


def fingerprint(state):
    content = {k: state[k] for k in ("url", "text", "actions", "scroll")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


class Browser:
    def __init__(
        self,
        url,
        *,
        user_data_dir=None,
        headless=False,
        channel=None,
        cdp_url=None,
        current_tab=False,
        tab_match=None,
    ):
        self._pw = sync_playwright().start()
        launch_kwargs = {"headless": headless}
        if channel:
            launch_kwargs["channel"] = channel
        self._persistent = bool(user_data_dir)
        self._cdp = bool(cdp_url)
        self._shared = False
        self._owns_page = True
        if cdp_url:
            # Attach to a Chrome the user already has open (existing tabs + logins).
            log.warning(
                "attaching over CDP - if Chrome asks to allow remote debugging, click Allow"
            )
            self._pw, self.browser = connect_cdp(cdp_url)
            if not self.browser.contexts:
                raise RuntimeError(
                    "Connected over CDP but found no browser context. Is Chrome running?"
                )
            self.context = self.browser.contexts[0]
            self.page = None
            if tab_match:
                self.page = self._find_tab(tab_match)
                if self.page is None:
                    log.warning("no tab matching %r; opening a new one", tab_match)
            elif current_tab and self.context.pages:
                self.page = self.context.pages[-1]
            if self.page is not None:
                self._owns_page = False
            else:
                self.page = self.context.new_page()
        elif self._persistent:
            self.browser = None
            self.context = self._pw.chromium.launch_persistent_context(
                user_data_dir,
                viewport={"width": 1120, "height": 780},
                **launch_kwargs,
            )
            self.page = (
                self.context.pages[0] if self.context.pages else self.context.new_page()
            )
        else:
            self.browser = self._pw.chromium.launch(**launch_kwargs)
            self.context = self.browser.new_context(
                viewport={"width": 1120, "height": 780}
            )
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.set_default_timeout(8000)
        self.after_input = None
        mode = "cdp" if self._cdp else "persistent" if self._persistent else "launch"
        log.debug("browser ready (%s) at %s", mode, url)
        self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            self.page.wait_for_load_state("load", timeout=15000)
        except PWTimeout:
            pass
        self._wait_until_interactive()

    def _wait_until_interactive(self, timeout=12):
        """Poll until the page exposes at least one actionable element.

        SPAs (WhatsApp Web, x.com, ...) render after 'load', so observing too
        early shows an empty page and the model wrongly chooses BLOCKED.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                info = self.evaluate(READ_STATE)
            except Exception:  # noqa: BLE001 - still navigating
                info = None
            if info and any(a["id"].startswith("e") for a in info["actions"]):
                return True
            time.sleep(0.3)
        log.debug("page never became interactive within %ds", timeout)
        return False

    def evaluate(self, expression, arg=None):
        try:
            return self.page.evaluate(expression, arg)
        except Exception as exc:  # noqa: BLE001 - map navigation races to StalePage
            message = str(exc)
            if any(
                token in message
                for token in ("Execution context was destroyed", "Cannot find context", "navigat")
            ):
                raise StalePage("Document changed during evaluation") from exc
            raise

    def observe(self, screenshot=True):
        if self.after_input:
            action, self.after_input = self.after_input, None
            # Read-only, and it happens after execution was logged, even if navigation interrupts it.
            try:
                self.evaluate(AFTER_INPUT, action)
            except Exception:  # noqa: BLE001 - settling is best effort
                pass
        for attempt in range(10):
            try:
                info = self.evaluate(READ_STATE)
                if info is None:
                    raise StalePage("Document is navigating")
                info["fingerprint"] = fingerprint(info)
                log.debug(
                    "observed %d actions, %d text chars on %s",
                    len(info["actions"]),
                    len(info["text"]),
                    info["url"],
                )
                if screenshot:
                    raw = self.page.screenshot(type="jpeg", quality=72)
                    info["screenshot"] = base64.b64encode(raw).decode()
                return info
            except StalePage:
                if attempt == 9:
                    raise
                time.sleep(0.02)
        raise StalePage("Page did not settle")

    def fresh(self, page, action=None):
        if action is not None and action["kind"] in {"click", "select", "upload"}:
            node = action["node"]
            if type(node) is not int:
                return False
            current = self.evaluate(GUARD, node)
            return current == [page["page_key"], page["guards"].get(str(node))]
        return self.evaluate(MARKER) == page["marker"]

    def _resolve(self, node):
        handle = self.evaluate_handle("(id) => window.__jiffy?.nodes.get(id) ?? null", node)
        element = handle.as_element()
        if element is None:
            raise StalePage("Target is gone. Observe again.")
        return element

    def evaluate_handle(self, expression, arg=None):
        return self.page.evaluate_handle(expression, arg)

    def act(self, action, page, text=None, file_path=None):
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        kind = action["kind"]
        log.debug("executing %s %s on %s", kind, action.get("id"), action.get("label"))
        if kind == "wait":
            time.sleep(0.1)
            self.after_input = None
            return {"executed": action["id"]}
        if kind == "scroll":
            self.page.mouse.wheel(0, action["delta"])
            self.after_input = action
            time.sleep(action_delay())
            return {"executed": action["id"]}
        node = action["node"]
        if type(node) is not int:
            raise ValueError("Invalid observed node")
        element = self._resolve(node)
        pages_before = len(self.context.pages)
        if kind == "upload":
            if not file_path:
                raise ValueError("No file configured for this UPLOAD target.")
            element.set_input_files(file_path)
        elif kind == "select":
            element.select_option(action["value"])
        elif kind == "fill":
            if not self.evaluate(HITTEST, action):
                raise StalePage("Target changed or is covered. Observe again.")
            element.fill(text or "")
        else:
            if not self.evaluate(HITTEST, action):
                raise StalePage("Target changed or is covered. Observe again.")
            element.click()
        self._adopt_new_tab(pages_before)
        self.after_input = action
        time.sleep(action_delay())
        return {"executed": action["id"]}

    def _adopt_new_tab(self, pages_before):
        """If the action opened a tab, follow it. Many sites (LinkedIn Message,
        Google results) open a new tab, and staying on the old page stalls."""
        try:
            pages = self.context.pages
        except Exception:  # noqa: BLE001
            return
        if len(pages) <= pages_before:
            return
        self.page = pages[-1]
        self._owns_page = True
        self.page.set_default_timeout(8000)
        try:
            self.page.wait_for_load_state("load", timeout=10000)
        except PWTimeout:
            pass
        log.info("action opened a new tab; switched to it")

    def _start_allow_clicker(self):
        """Background thread that clicks Chrome's Allow sheet while we connect."""
        return start_allow_clicker()

    def _find_tab(self, needle):
        """Find an existing tab across all contexts whose URL contains needle."""
        needle = needle.lower()
        for context in self.browser.contexts:
            for page in context.pages:
                try:
                    url = page.url or ""
                except Exception:  # noqa: BLE001 - closed tabs can throw
                    continue
                if needle in url.lower():
                    self.context = context
                    return page
        return None

    def close(self):
        try:
            if self._cdp or self._shared:
                # Never close a browser/context we do not own; only our tab.
                if self._owns_page:
                    try:
                        self.page.close()
                    except Exception:  # noqa: BLE001
                        pass
            elif self._persistent:
                self.context.close()
            elif self.browser:
                self.browser.close()
        finally:
            # A shared connection (from_cdp / from_context) has no Playwright instance.
            if self._pw is not None:
                self._pw.stop()

    @classmethod
    def from_context(cls, context, url, *, current_tab=False, tab_match=None):
        """Build a Browser on an existing persistent context, opening a new page."""
        self = cls.__new__(cls)
        self._pw = None
        self.browser = None
        self.context = context
        self._persistent = True
        self._cdp = False
        self._shared = True
        self._owns_page = True
        self.page = None
        if tab_match:
            self.page = self._find_tab(tab_match)
        elif current_tab and context.pages:
            self.page = context.pages[-1]
        if self.page is not None:
            self._owns_page = False
        else:
            self.page = context.new_page()
        self.page.set_default_timeout(8000)
        self.after_input = None
        self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            self.page.wait_for_load_state("load", timeout=15000)
        except PWTimeout:
            pass
        self._wait_until_interactive()
        return self

    @classmethod
    def from_cdp(cls, cdp_browser, url, *, current_tab=False, tab_match=None):
        """Build a Browser on an existing CDP connection, opening a new page.

        Used by the daemon so every run reuses one long-lived connection (and
        therefore one Chrome "Allow" prompt per browser session).
        """
        self = cls.__new__(cls)
        self._pw = None
        self.browser = cdp_browser
        self._persistent = False
        self._cdp = True
        self._owns_page = True
        if not cdp_browser.contexts:
            raise RuntimeError("CDP connection has no browser context")
        self.context = cdp_browser.contexts[0]
        self.page = None
        if tab_match:
            self.page = self._find_tab(tab_match)
        elif current_tab and self.context.pages:
            self.page = self.context.pages[-1]
        if self.page is not None:
            self._owns_page = False
        else:
            self.page = self.context.new_page()
        self.page.set_default_timeout(8000)
        self.after_input = None
        self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            self.page.wait_for_load_state("load", timeout=15000)
        except PWTimeout:
            pass
        self._wait_until_interactive()
        return self
