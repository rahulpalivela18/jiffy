"""Per-site skills: short instructions for how a specific website works.

Complex sites (WhatsApp mentions, Gmail compose, LinkedIn Easy Apply) have
stateful flows that generic rules cannot guess. A skill file is plain text that
gets injected into Jev's instructions for that site.

Layout:
    skills/<host>.md          e.g. skills/web.whatsapp.com.md
    skills/<domain>.md        e.g. skills/whatsapp.com.md  (fallback)

Resolution: exact host first, then the registrable domain.
"""

from pathlib import Path
from urllib.parse import urlparse

SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"


def _candidates(host):
    host = (host or "").lower()
    if not host:
        return []
    parts = host.split(".")
    out = [host]
    if len(parts) > 2:
        out.append(".".join(parts[-2:]))
    return out


def skill_for_url(url, directory=None):
    """Return the skill text for a URL's host, or None."""
    directory = Path(directory or SKILLS_DIR)
    if not directory.is_dir():
        return None
    host = urlparse(url or "").hostname or ""
    for name in _candidates(host):
        path = directory / f"{name}.md"
        if path.exists():
            text = path.read_text(errors="replace").strip()
            if text:
                return text
    return None


def skill_from_path(path):
    if not path:
        return None
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"skill file not found: {p}")
    return p.read_text(errors="replace").strip()
