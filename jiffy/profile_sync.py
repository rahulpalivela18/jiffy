"""Sync a Chrome profile into the dedicated automation profile.

The dedicated automation Chrome uses a non-standard user-data-dir, which is the
only way to avoid Chrome 136+/144+'s "Allow remote debugging?" dialog. But a
fresh profile means no logins and default theme.

This copies the useful parts from your real Chrome profile:
  * Local State + Preferences  -> theme and UI settings
  * Cookies (SQLite)           -> sign-in sessions
  * Web Data (SQLite)          -> autofill / form history

Only these files are touched. Passwords are never copied.
"""

import json
import shutil
import sqlite3
from pathlib import Path

from .browser import _CDP_PROFILE_DIRS
from .log import get as _get_logger

log = _get_logger("profile_sync")

_JSON_FILES = ("Local State",)
_SQLITE_FILES = ("Default/Cookies", "Default/Web Data")
_PREF_FILES = ("Default/Preferences", "Default/Secure Preferences")


def real_chrome_root():
    """The user-data-dir of the real Chrome profile, if present."""
    best = None
    for rel in _CDP_PROFILE_DIRS:
        base = Path.home() / rel
        if (base / "Default").is_dir() and (base / "Local State").exists():
            # Prefer the one with the most cookies.
            cookies = base / "Default" / "Cookies"
            size = cookies.stat().st_size if cookies.exists() else 0
            if best is None or size > best[1]:
                best = (base, size)
    return best[0] if best else None


def _sqlite_copy(src, dst):
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    target = sqlite3.connect(str(dst))
    try:
        with target:
            source.backup(target)
    finally:
        target.close()
        source.close()


def sync_into(dest_root, source_root=None):
    """Copy cookies, prefs, and theme from source_root into dest_root.

    Returns the list of relative paths copied. Safe to call repeatedly.
    """
    source_root = Path(source_root or real_chrome_root())
    dest_root = Path(dest_root)
    if not source_root or not source_root.exists():
        return []
    (dest_root / "Default").mkdir(parents=True, exist_ok=True)
    copied = []

    for rel in (*_JSON_FILES, *_PREF_FILES):
        src = source_root / rel
        if not src.exists():
            continue
        try:
            shutil.copy2(src, dest_root / rel)
            copied.append(rel)
        except OSError as exc:
            log.debug("could not copy %s: %s", rel, exc)

    for rel in _SQLITE_FILES:
        src = source_root / rel
        if not src.exists():
            continue
        try:
            _sqlite_copy(src, dest_root / rel)
            copied.append(rel)
        except (sqlite3.Error, OSError) as exc:
            log.debug("could not copy %s: %s", rel, exc)

    if copied:
        log.info("synced %d files from %s", len(copied), source_root)
    return copied


def theme_of(root):
    """Best-effort read of the browser color scheme from a profile's Local State."""
    try:
        state = json.loads((Path(root) / "Local State").read_text(errors="replace"))
    except (OSError, ValueError):
        return None
    return (state.get("browser") or {}).get("theme", {}).get("color_scheme")
