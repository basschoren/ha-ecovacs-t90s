"""Robot commands that deebot-client 18.7.0 does not have (or builds wrongly).

They are small deebot-client ``Command`` subclasses, so they go through the
normal device queue and the shared login. See the modules for the payloads
and where they come from.
"""

from .base import CommandOutcome, T90Command, classify_response

__all__ = ["CommandOutcome", "T90Command", "classify_response"]
