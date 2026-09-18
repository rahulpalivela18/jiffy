"""Click Chrome's per-connection "Allow remote debugging?" sheet on macOS.

This mirrors browser-harness's `mac-approve`. Chrome 144+ shows a sheet for each
CDP connection and only the connection that raised it keeps it on screen, so we
click it from a background thread while the connection attempt is live.

Requires the app launching jiffy (Terminal, iTerm, VS Code, ...) to have
System Settings > Privacy & Security > Accessibility permission.
"""

import platform
import subprocess

PROCESS_NAMES = (
    "Google Chrome",
    "Google Chrome 2",
    "Google Chrome Beta",
    "Google Chrome Canary",
    "Chromium",
    "Brave Browser",
    "Microsoft Edge",
)

_SCRIPT = r'''using terms from application "System Events"
    on clickAllow(nodeRef)
        try
            if (role of nodeRef as text) is "AXButton" and ¬
                ((description of nodeRef as text) is "Allow" or (name of nodeRef as text) is "Allow") then
                -- Chrome ignores synthetic AXPress for this consent sheet; send a
                -- real click at the button's screen position instead.
                try
                    set p to position of nodeRef
                    set s to size of nodeRef
                    set cx to (item 1 of p) + ((item 1 of s) / 2)
                    set cy to (item 2 of p) + ((item 2 of s) / 2)
                    click at {cx, cy}
                    return true
                end try
                perform action "AXPress" of nodeRef
                return true
            end if
        end try
        try
            repeat with childRef in UI elements of nodeRef
                if my clickAllow(childRef) then return true
            end repeat
        end try
        return false
    end clickAllow
end using terms from

set resultText to "not-found"
set nameList to __NAMES__
tell application "System Events"
    repeat with procRef in nameList
        set pName to contents of procRef as text
        if exists process pName then
            tell process pName
                repeat with w in windows
                    try
                        set wName to name of w as text
                    on error
                        set wName to ""
                    end try
                    if wName contains "remote debugging" then
                        if my clickAllow(w) then
                            set resultText to "ready"
                            exit repeat
                        end if
                    end if
                    try
                        repeat with s in sheets of w
                            try
                                set sName to name of s as text
                            on error
                                set sName to ""
                            end try
                            if sName contains "remote debugging" then
                                if my clickAllow(s) then
                                    set resultText to "ready"
                                    exit repeat
                                end if
                            end if
                        end repeat
                    end try
                    if resultText is "ready" then exit repeat
                end repeat
            end tell
        end if
        if resultText is "ready" then exit repeat
    end repeat
end tell
return resultText
'''


def _names_literal():
    return "{" + ", ".join(f'"{name}"' for name in PROCESS_NAMES) + "}"


def _run(script, timeout=15):
    try:
        return subprocess.run(
            ["osascript"],
            input=script,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None
    except (OSError, subprocess.SubprocessError):
        return None


def approve_once():
    """Try once to click the sheet.

    Returns 'ready' | 'not-found' | 'accessibility-required' | 'error' | 'unsupported'.
    """
    if platform.system() != "Darwin":
        return "unsupported"
    completed = _run(_SCRIPT.replace("__NAMES__", _names_literal()))
    if completed is None:
        return "error"
    if completed.returncode != 0:
        detail = (completed.stderr or "").lower()
        if "assistive" in detail or "not allowed" in detail or "(-25211)" in detail:
            return "accessibility-required"
        return "error"
    return completed.stdout.strip() or "error"


def accessibility_granted():
    """True when osascript can actually drive the UI (Accessibility set).

    `UI elements enabled` is the authoritative flag for the calling app.
    Probing a specific process's window is unreliable (e.g. Finder may have no
    windows) and gives false negatives.
    """
    if platform.system() != "Darwin":
        return True
    probe = 'tell application "System Events" to return UI elements enabled'
    completed = _run(probe, timeout=5)
    if completed is None or completed.returncode != 0:
        return False
    return completed.stdout.strip().lower() == "true"
