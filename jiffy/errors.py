"""Shared errors."""


class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


class NeedsUser(Exception):
    """The agent cannot proceed safely and needs a human (upload, captcha, low confidence)."""
