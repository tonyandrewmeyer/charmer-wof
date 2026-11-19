"""The scoring ladder (PLAN §4): static, hidden oracle, live, judge, process.

Every run goes all the way up: the earlier tiers are diagnostics, not gates.

``SCORING_CODE_VERSION`` is part of the ``series`` key (PLAN §1.1). Bump it
whenever a change here could move a score, tag or metric for the same agent
output; don't bump it for refactors that can't.
"""

SCORING_CODE_VERSION = "2"
"""2 (2026-11-16): the key-oracle-test-failed, no-edits and only-vendored-libs-changed tags."""


def scoring_code_version(charmlint: str) -> str:
    """The version recorded in a run: this code plus the static tier's pinned tools."""
    return f"{SCORING_CODE_VERSION}+charmlint-{charmlint}"
