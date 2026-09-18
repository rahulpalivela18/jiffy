"""Command line entry point: run one goal, with an optional starting URL."""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

from .agent import Agent
from .questions import MAX_STEPS

load_dotenv()

DEFAULT_START_URL = os.getenv("JIFFY_START_URL", "https://www.google.com")
DAEMON_URL = os.getenv("JIFFY_DAEMON_URL", "http://127.0.0.1:8765")
ROOT = Path(__file__).resolve().parents[1]
GOAL_FILE = Path(os.getenv("JIFFY_GOAL_FILE") or (ROOT / "goal.txt"))


def read_goal_file():
    """Read the goal from goal.txt. Lines starting with # are comments."""
    if not GOAL_FILE.exists():
        return None
    lines = [
        line
        for line in GOAL_FILE.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    goal = "\n".join(lines).strip()
    return goal or None


def _get_json(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read())


def _post_json(url, payload, timeout=1800):
    data = json.dumps(payload).encode()
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def daemon_health():
    try:
        return _get_json(f"{DAEMON_URL}/health", timeout=2)
    except Exception:  # noqa: BLE001 - not running counts as unhealthy
        return None


def ensure_daemon(cdp_url, wait=60):
    """Return healthy daemon state, starting one only if none is reachable.

    Never spawns a second daemon while one is up: a duplicate would fail to bind
    the port, exit, and take the shared browser down with it.
    """
    health = daemon_health()
    if health and health.get("ready"):
        return health
    if health is not None and health.get("error"):
        # A daemon is up but its browser failed. Restart it cleanly.
        try:
            _post_json(f"{DAEMON_URL}/shutdown", {}, timeout=3)
        except Exception:  # noqa: BLE001
            pass
        subprocess.run(["pkill", "-f", "jiffy.daemon"], capture_output=True)
        deadline = time.time() + 8
        while time.time() < deadline and daemon_health() is not None:
            time.sleep(0.3)
        health = None

    if health is None:
        env = {**os.environ}
        if cdp_url:
            env["JIFFY_CDP_URL"] = cdp_url
        log_path = Path.home() / ".jiffy" / "daemon.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "a", encoding="utf-8")
        subprocess.Popen(
            [sys.executable, "-m", "jiffy.daemon"],
            env=env,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )
        print("starting the browser daemon...", file=sys.stderr)
        # Wait for the port to come up before talking to it.
        deadline = time.time() + 15
        while time.time() < deadline and daemon_health() is None:
            time.sleep(0.3)

    # Ask the daemon to connect (launches Chrome, or waits for Allow).
    try:
        return _post_json(f"{DAEMON_URL}/connect", {}, timeout=wait + 90)
    except Exception as exc:  # noqa: BLE001 - report the failure
        return {"ready": False, "error": str(exc)}


def url_from_goal(goal):
    """If the goal already contains a URL, start there. Deterministic, no model."""
    match = re.search(r"https?://[^\s\"'<>]+", goal or "")
    return match.group(0).rstrip(".,);") if match else None


def resolve_start_url(goal, url=None):
    """Pick where to start: explicit URL, a URL in the goal, an LLM-resolved site, else search."""
    if url:
        return url
    explicit = url_from_goal(goal)
    if explicit:
        return explicit
    try:
        from .model import start_url_for_goal

        resolved = start_url_for_goal(goal)
    except Exception:  # noqa: BLE001 - resolution is best effort
        resolved = None
    return resolved or DEFAULT_START_URL


def build_parser():
    parser = argparse.ArgumentParser(
        prog="jiffy",
        description="A general Chrome agent. Jev decides, a cheap LLM writes text, code executes.",
    )
    parser.add_argument("goal", nargs="?", help="What the agent should accomplish in plain language.")
    parser.add_argument(
        "--url",
        help="Page to start from. Optional: a URL in the goal is used, else a search page.",
    )
    parser.add_argument("--resume", help="Local file to use for UPLOAD targets (e.g. resume.pdf).")
    parser.add_argument(
        "--profile",
        default=os.getenv("JIFFY_PROFILE") or None,
        help="YAML/JSON profile of personal facts for filling fields.",
    )
    parser.add_argument(
        "--skill",
        default=os.getenv("JIFFY_SKILL") or None,
        help="Path to a site-skill markdown file (else skills/<host>.md is used).",
    )
    parser.add_argument("--max-steps", type=int, default=int(os.getenv("JIFFY_MAX_STEPS", MAX_STEPS)))
    parser.add_argument(
        "--confidence-floor",
        type=float,
        default=float(os.getenv("JIFFY_CONFIDENCE_FLOOR", "0.5")),
        help="Stop and ask the user below this confidence.",
    )
    parser.add_argument("--record", help="Directory to save step screenshots.")
    parser.add_argument("--headless", action="store_true", default=os.getenv("JIFFY_HEADLESS", "false") == "true")
    parser.add_argument("--channel", default=os.getenv("JIFFY_CHANNEL", "chrome"))
    parser.add_argument("--user-data-dir", default=os.getenv("JIFFY_USER_DATA_DIR") or None)
    parser.add_argument(
        "--cdp-url",
        default=os.getenv("JIFFY_CDP_URL") or None,
        help="Attach to an already-running Chrome, e.g. http://localhost:9222",
    )
    parser.add_argument(
        "--attach",
        action="store_true",
        help="Auto-discover and attach to your already-running Chrome (logins stay).",
    )
    parser.add_argument(
        "--launch-chrome",
        action="store_true",
        help="Launch a dedicated Chrome with remote debugging (no Allow dialog; logins persist there).",
    )
    parser.add_argument(
        "--attach-wait",
        type=int,
        default=30,
        help="Seconds to wait for remote debugging after opening the toggle page.",
    )
    parser.add_argument(
        "--tab-match",
        default=None,
        help="Attach to an existing tab whose URL contains this text (with --attach/--cdp-url).",
    )
    parser.add_argument(
        "--current-tab",
        action="store_true",
        help="With --cdp-url, drive the last existing tab instead of opening a new one.",
    )
    parser.add_argument(
        "--log",
        default=os.getenv("JIFFY_LOG", "normal"),
        choices=["quiet", "normal", "verbose"],
        help="Log verbosity.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Never prompt; useful for scripts and non-interactive runs.",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Print browser-connection diagnostics and exit.",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Run the persistent browser daemon in the foreground.",
    )
    parser.add_argument(
        "--stop-daemon",
        action="store_true",
        help="Stop a running browser daemon and exit.",
    )
    parser.add_argument(
        "--no-daemon",
        action="store_true",
        help="Connect directly instead of reusing the persistent daemon.",
    )
    parser.add_argument(
        "--daemon-wait",
        type=int,
        default=60,
        help="Seconds to wait for the daemon to connect (and Allow click).",
    )
    parser.add_argument(
        "--approve",
        action="store_true",
        help="Click Chrome's 'Allow remote debugging?' sheet once and exit.",
    )
    parser.add_argument("--quiet", action="store_true", help="Only print the final status.")
    return parser


def _top_probs(probabilities, top=2):
    if not probabilities:
        return ""
    ranked = sorted(probabilities.items(), key=lambda item: item[1], reverse=True)[:top]
    return " ".join(f"{key}={value:.2f}" for key, value in ranked)


def _format_step(step):
    line = (
        f"[{step['elapsed_ms']:>6}ms] {step['operation']:<9} {step['kind']:<7} "
        f"{step['action'][:50]}"
    )
    if step.get("text"):
        line += f"  -> {step['text']!r}"
    line += f"  conf={step.get('confidence', 0):.2f}"
    op_probs = _top_probs(step.get("operation_probabilities"))
    if op_probs:
        line += f"  op[{op_probs}]"
    target_probs = _top_probs(step.get("target_probabilities"))
    if target_probs:
        line += f"  tgt[{target_probs}]"
    return line


def _confirm(message, assume_yes):
    """Wait for the user to act, unless running non-interactively or with --yes."""
    if assume_yes or not sys.stdin.isatty():
        return
    try:
        input(message)
    except EOFError:
        pass


def login_command():
    """Open the jiffy Chrome on a normal page so the user can log in once."""
    health = ensure_daemon(None, wait=90)
    if not health.get("ready"):
        print(f"could not start the browser: {health.get('error')}", file=sys.stderr)
        return 1
    try:
        _post_json(f"{DAEMON_URL}/open", {"url": "https://www.google.com"}, timeout=120)
    except Exception as exc:  # noqa: BLE001 - clear message for the user
        print(f"could not open a page: {exc}", file=sys.stderr)
        return 1
    print("Opened the jiffy Chrome.")
    print("Log into the sites you need there (once), then run:")
    print('  ./bin/jiffy "your goal"')
    return 0


SUBCOMMANDS = ("setup", "doctor", "stop", "serve", "login", "run")


def _print_new_steps(final, printed):
    history = final.get("history", [])
    for step in history[printed:]:
        print(_format_step(step))
    return len(history)


def run_via_daemon(args, start_url, profile):
    """Run a goal through the daemon, pausing interactively when the agent is unsure."""
    body = {
        "goal": args.goal,
        "url": start_url,
        "resume": args.resume,
        "profile": profile,
        "max_steps": args.max_steps,
        "confidence_floor": args.confidence_floor,
        "current_tab": args.current_tab,
        "tab_match": args.tab_match,
        "skill": args.skill,
    }
    final = _post_json(f"{DAEMON_URL}/run", body)
    from .log import get as get_logger

    get_logger("cli").info("confidence floor: %.2f", args.confidence_floor)
    printed = 0 if args.quiet else _print_new_steps(final, 0)

    interactive = sys.stdin.isatty() and not args.yes and not args.quiet
    while final.get("status") == "needs_user" and interactive:
        print(f"\nneeds user: {final.get('needs_user')}")
        print("  c = continue anyway   g = give guidance   q = quit   (Enter = continue)")
        try:
            choice = input("choice [c]: ").strip().lower()
        except EOFError:
            break
        if choice.startswith("q"):
            break
        resume_body = {"action": "continue"}
        if choice.startswith("g"):
            try:
                resume_body = {"action": "guidance", "text": input("guidance: ")}
            except EOFError:
                break
        try:
            final = _post_json(f"{DAEMON_URL}/resume", resume_body)
        except Exception as exc:  # noqa: BLE001 - surface daemon errors
            print(f"resume failed: {exc}", file=sys.stderr)
            break
        printed = _print_new_steps(final, printed)

    print(f"\nstatus: {final['status']}")
    if final.get("needs_user"):
        print(f"needs user: {final['needs_user']}")
    print(f"final url: {final['page']['url']}")
    print(f"steps: {len(final.get('history', []))}  time: {final['elapsed_ms']}ms")
    return 0 if final["status"] == "done" else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in SUBCOMMANDS:
        command = argv.pop(0)
        if command == "setup":
            from .setup import main as setup_main

            return setup_main(argv)
        if command == "login":
            return login_command()
        if command == "doctor":
            argv = ["--doctor", *argv]
        elif command == "stop":
            argv = ["--stop-daemon", *argv]
        elif command == "serve":
            argv = ["--serve", *argv]
        # "run" just falls through with the remaining args as the goal.

    args = build_parser().parse_args(argv)

    if args.doctor:
        from .approve import accessibility_granted
        from .browser import doctor

        info = doctor()
        toggle = info["remote_debugging_toggle"]
        print("jiffy doctor")
        print(f"  chrome executable       : {info['chrome_executable'] or 'NOT FOUND'}")
        print(
            "  remote-debugging toggle : "
            + ("on" if toggle is True else "off" if toggle is False else "unknown")
        )
        print(f"  CDP endpoint            : {info['endpoint'] or 'none'}")
        print(f"  configured (JIFFY_CDP_URL): {os.getenv('JIFFY_CDP_URL') or 'none'}")
        print(
            "  accessibility (macOS)   : "
            + ("granted" if accessibility_granted() else "NOT granted")
        )
        print(
            f"  automation profile      : {info['automation_profile']} "
            f"({'exists' if info['automation_profile_exists'] else 'not created'})"
        )
        if not info["endpoint"]:
            print("\nTo attach to your Chrome:")
            print("  1. open chrome://inspect/#remote-debugging and tick the box")
            print("  2. run `jiffy --approve` to click Allow for you")
        return 0

    if args.approve:
        from .approve import accessibility_granted, approve_once

        if not accessibility_granted():
            print(
                "Grant Accessibility permission to this terminal in System Settings > "
                "Privacy & Security > Accessibility, then retry.",
                file=sys.stderr,
            )
            return 2
        print(approve_once())
        return 0

    if args.serve:
        from .daemon import main as daemon_main

        daemon_main()
        return 0

    if args.stop_daemon:
        try:
            _post_json(f"{DAEMON_URL}/shutdown", {}, timeout=5)
        except Exception:  # noqa: BLE001 - fall back to killing it
            subprocess.run(["pkill", "-f", "jiffy.daemon"], capture_output=True)
        print("daemon stopped")
        return 0

    if not args.goal:
        file_goal = read_goal_file()
        if file_goal:
            args.goal = file_goal
            print(f"goal (from {GOAL_FILE.name}): {args.goal.splitlines()[0][:80]}", file=sys.stderr)

    if not args.goal:
        if GOAL_FILE.exists():
            print(
                f"{GOAL_FILE} is empty. Add a goal to it, then run: ./bin/jiffy run",
                file=sys.stderr,
            )
        else:
            print(
                'Usage: jiffy "your goal"  |  jiffy run  (reads goal.txt)  |  '
                "jiffy setup | login | doctor | stop",
                file=sys.stderr,
            )
        return 2

    start_url = resolve_start_url(args.goal, args.url)

    if not os.getenv("TYPESAFE_API_KEY"):
        print("Missing TYPESAFE_API_KEY (see .env.example).", file=sys.stderr)
        return 2

    from .browser import (
        Browser,
        discover_cdp_endpoint,
        open_remote_debugging_page,
        wait_for_cdp,
    )
    from .log import get as get_logger
    from .log import setup as setup_logging
    from .profile import load_profile

    setup_logging(args.log, quiet=args.quiet)
    log = get_logger("cli")

    cdp_url = args.cdp_url
    launched = False
    if not cdp_url and args.launch_chrome:
        from .browser import launch_automation_chrome

        try:
            cdp_url = launch_automation_chrome()
        except Exception as exc:  # noqa: BLE001 - surface a clear message
            print(f"Could not launch Chrome: {exc}", file=sys.stderr)
            return 2
        launched = True
        log.info("launched dedicated Chrome at %s", cdp_url)
    if not cdp_url and args.attach:
        cdp_url = discover_cdp_endpoint()
        if not cdp_url:
            app = open_remote_debugging_page()
            if app:
                log.info("opened chrome://inspect/#remote-debugging in %s", app)
            print(
                "In Chrome, tick 'Allow remote debugging for this browser instance'.",
                file=sys.stderr,
            )
            _confirm("Press Enter once you have ticked it: ", args.yes)
            cdp_url = wait_for_cdp(timeout=args.attach_wait)
            if not cdp_url:
                print(
                    "Still no remote-debugging endpoint. Enable it in Chrome and retry.",
                    file=sys.stderr,
                )
                return 2
        log.info("discovered Chrome at %s", cdp_url)

    profile = {}
    if args.profile:
        try:
            profile = load_profile(args.profile)
        except Exception as exc:  # noqa: BLE001 - bad profile should not be silent
            print(f"Could not load profile {args.profile!r}: {exc}", file=sys.stderr)
            return 2
        resume_from_profile = profile.get("job.resume")
        if resume_from_profile and not args.resume:
            args.resume = os.path.expanduser(resume_from_profile)

    if not args.no_daemon and not args.user_data_dir:
        health = ensure_daemon(cdp_url, wait=args.daemon_wait)
        if not health.get("ready"):
            print(f"daemon not ready: {health.get('error')}", file=sys.stderr)
            return 2
        log.info("using daemon at %s", DAEMON_URL)
        return run_via_daemon(args, start_url, profile)

    log.info("goal: %s", args.goal)
    log.info("start url: %s", start_url)
    if cdp_url:
        log.info("attaching to existing Chrome at %s", cdp_url)
        if not launched:
            _confirm(
                "Chrome will ask to allow remote debugging for this connection.\n"
                "Press Enter here, then click Allow in Chrome: ",
                args.yes,
            )
    elif args.user_data_dir:
        log.info("using persistent profile at %s", args.user_data_dir)

    browser = Browser(
        start_url,
        user_data_dir=args.user_data_dir,
        headless=args.headless,
        channel=args.channel or None,
        cdp_url=cdp_url,
        current_tab=args.current_tab,
        tab_match=args.tab_match,
    )
    from .skills import skill_for_url, skill_from_path

    skill = skill_from_path(args.skill) if args.skill else skill_for_url(start_url)
    agent = Agent(
        start_url,
        args.goal,
        browser=browser,
        record_dir=args.record,
        resume=args.resume,
        profile=profile,
        skill=skill,
        max_steps=args.max_steps,
        confidence_floor=args.confidence_floor,
    )
    printed = 0
    try:
        for state in agent.run():
            if args.quiet:
                continue
            history = state.get("history", [])
            for step in history[printed:]:
                print(_format_step(step))
            printed = len(history)
        final = agent.snapshot()
        print(f"\nstatus: {final['status']}")
        if final.get("needs_user"):
            print(f"needs user: {final['needs_user']}")
        print(f"final url: {final['page']['url']}")
        print(f"steps: {len(final['history'])}  time: {final['elapsed_ms']}ms")
        return 0 if final["status"] == "done" else 1
    finally:
        agent.close()
        time.sleep(0.05)


if __name__ == "__main__":
    raise SystemExit(main())
