"""One-time setup: give jiffy a Chrome debugging server to talk to.

Run as `jiffy setup` (or `python setup_chrome.py`).

Default (recommended): launches a dedicated Chrome on a non-standard profile.
That is the only way to avoid Chrome 136+/144+'s per-connection
"Allow remote debugging?" dialog. Cookies/theme are copied once, but Chrome
does not honor copied cookies for auth, so log into sites once in the jiffy
window; that profile then persists.

`--attach-existing` instead tries to use the Chrome you already have open
(all your logins exactly as they are); Chrome will ask you to click Allow.
"""

import argparse
import subprocess
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

from .approve import accessibility_granted, approve_once
from .browser import (
    _endpoint_for_port,
    _port_live,
    check_endpoint,
)

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

ACCESSIBILITY_HELP = """
Chrome asks for one click per connection, and jiffy can click it for you only if
the app you run this from has Accessibility permission:

  System Settings > Privacy & Security > Accessibility
    -> add and enable: Terminal (or iTerm / VS Code / your editor)

Then run setup again. Or skip it and just click Allow in Chrome yourself.
""".rstrip()

DEDICATED_PORT = 9333


def endpoint_for(port):
    if not _port_live(port):
        return None
    return _endpoint_for_port(port) or f"http://127.0.0.1:{port}"


def activate_chrome():
    for app in (
        "Google Chrome 2",
        "Google Chrome",
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


def open_accessibility_settings():
    try:
        subprocess.run(
            [
                "open",
                "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
            ],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def connect_with_approval(endpoint, seconds=30):
    """Keep trying to connect while auto-clicking Allow when possible."""
    from playwright.sync_api import sync_playwright

    stop = threading.Event()
    state = {"accessibility": None, "clicked": False}

    def clicker():
        state["accessibility"] = accessibility_granted()
        if not state["accessibility"]:
            return
        deadline = time.time() + seconds
        while not stop.is_set() and time.time() < deadline:
            if approve_once() == "ready":
                state["clicked"] = True
                return
            time.sleep(0.4)

    threading.Thread(target=clicker, daemon=True).start()
    connected = False
    attempts = 0
    with sync_playwright() as pw:
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                browser = pw.chromium.connect_over_cdp(endpoint, timeout=8000)
                browser.close()
                connected = True
                break
            except Exception:  # noqa: BLE001 - retry until deadline
                attempts += 1
                if attempts % 3 == 0:
                    print(f"  still waiting for Allow... ({int(time.time())})", flush=True)
                time.sleep(0.5)
    stop.set()
    return connected, bool(state["accessibility"]), state["clicked"]


def write_env(endpoint):
    env = ROOT / ".env"
    lines = env.read_text().splitlines() if env.exists() else []
    lines = [line for line in lines if not line.startswith("JIFFY_CDP_URL=")]
    lines.append(f"JIFFY_CDP_URL={endpoint}")
    env.write_text("\n".join(lines).strip() + "\n")


def clear_env():
    env = ROOT / ".env"
    if not env.exists():
        return
    lines = [line for line in env.read_text().splitlines() if not line.startswith("JIFFY_CDP_URL=")]
    env.write_text("\n".join(lines).strip() + "\n")


def report_ready(endpoint):
    write_env(endpoint)
    print("\nReady.")
    print(f"  server : {endpoint}")
    print("  saved  : JIFFY_CDP_URL in .env")
    print("\nNext:")
    print("  1. Log into any sites you need in the jiffy Chrome window")
    print("     (Google, LinkedIn, ...) ONCE. It stays logged in.")
    print("  2. Then just run:")
    print('       jiffy "your goal"')
    print("\nNo more Allow dialogs, ever.")


def setup_dedicated(sync):
    profile = Path.home() / ".jiffy" / "chrome-profile"
    profile.mkdir(parents=True, exist_ok=True)
    if sync:
        try:
            from .profile_sync import sync_into

            sync_into(profile)
        except Exception as exc:  # noqa: BLE001 - sync is best effort
            print(f"profile sync skipped: {exc}")
    # Dedicated mode does not use CDP; make sure no stale endpoint lingers.
    clear_env()
    print("\nReady.")
    print("The jiffy Chrome launches on demand (no Allow dialogs).")
    print("\nNext:")
    print("  1. ./bin/jiffy login      # open it and log into sites once")
    print("  2. ./bin/jiffy run        # run the goal in goal.txt")
    return 0


def setup_existing(port, wait):
    endpoint = endpoint_for(port)
    if not endpoint:
        print(f"nothing is listening on port {port}; using the jiffy Chrome instead")
        return setup_dedicated(sync=True)
    print(f"found Chrome on port {port}: {endpoint}")
    if check_endpoint(endpoint):
        report_ready(endpoint)
        return 0
    print("Chrome is asking to allow the connection.")
    app = activate_chrome()
    if app:
        print(f"brought {app} to the front.")
    print(f"Click Allow in Chrome now (waiting up to {wait}s).")
    print("No dialog? Open chrome://inspect/#remote-debugging in Chrome.")
    connected, can_click, clicked = connect_with_approval(endpoint, seconds=wait)
    if connected:
        if clicked:
            print("clicked Allow for you.")
        report_ready(endpoint)
        return 0
    if not can_click:
        print(ACCESSIBILITY_HELP)
        open_accessibility_settings()
    print(
        f"\nCould not attach to the Chrome on port {port}.\n"
        "Run `jiffy setup` without --attach-existing to use the jiffy Chrome."
    )
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="jiffy setup", description="Set up the jiffy browser.")
    parser.add_argument("--port", type=int, default=9222, help="CDP port (default 9222).")
    parser.add_argument("--wait", type=int, default=30, help="Seconds to wait for Allow.")
    parser.add_argument(
        "--attach-existing",
        action="store_true",
        help="Use the Chrome you already have open instead of a dedicated one.",
    )
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help="Do not copy cookies/theme into the dedicated profile.",
    )
    args = parser.parse_args(argv)

    print("jiffy setup")
    if args.attach_existing:
        return setup_existing(args.port, args.wait)
    return setup_dedicated(sync=not args.no_sync)
