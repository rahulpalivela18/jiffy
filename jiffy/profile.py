"""Tiny retrieval layer over a personal profile.

The text helper must never invent personal information. Instead we keep a small
fact store (profile.yaml) and inject the facts that are relevant to the field
being filled. This is deliberately simple retrieval, not a vector store: a
profile is a few dozen facts, so keyword overlap is enough and stays inspectable.
"""

import json
import re
from pathlib import Path

# Facts that are almost always safe and useful to include.
CORE_KEYS = (
    "name",
    "first_name",
    "last_name",
    "email",
    "phone",
    "location",
    "linkedin",
    "github",
    "website",
)

_TOKEN = re.compile(r"[a-z0-9]+")


def load_profile(path):
    """Load a flat or nested YAML/JSON profile into a flat {key: value} dict."""
    raw = Path(path).read_text()
    if str(path).endswith((".yaml", ".yml")):
        import yaml

        data = yaml.safe_load(raw) or {}
    else:
        data = json.loads(raw or "{}")
    return flatten(data)


def flatten(data, prefix=""):
    flat = {}
    for key, value in (data or {}).items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            flat.update(flatten(value, name))
        elif isinstance(value, list):
            flat[name] = "; ".join(str(v) for v in value)
        elif value is not None:
            flat[name] = str(value)
    return flat


def _tokens(text):
    return set(_TOKEN.findall((text or "").lower()))


def retrieve(profile, query, limit=14):
    """Return the most relevant facts as a {key: value} dict.

    Core identity keys are always included. Everything else is ranked by token
    overlap with the field label + page text.
    """
    if not profile:
        return {}
    query_tokens = _tokens(query)
    scored = []
    for key, value in profile.items():
        leaf = key.rsplit(".", 1)[-1].lower()
        overlap = len((_tokens(key) | _tokens(leaf) | _tokens(value)) & query_tokens)
        is_core = leaf in CORE_KEYS
        scored.append((is_core, overlap, key, value))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    facts = {}
    for is_core, _overlap, key, value in scored[:limit]:
        facts[key] = value
    return facts
