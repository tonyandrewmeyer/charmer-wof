"""The scoring ladder (PLAN §4): static, hidden oracle, live, judge, process.

Every run goes all the way up: the earlier tiers are diagnostics, not gates.

``SCORING_CODE_VERSION`` is part of the ``series`` key (PLAN §1.1). Bump it
whenever a change here could move a score, tag or metric for the same agent
output; don't bump it for refactors that can't.
"""

SCORING_CODE_VERSION = "6"
"""2 (2026-11-16): the key-oracle-test-failed, no-edits and only-vendored-libs-changed tags.
3 (2026-11-19): pack (``scoring.pack``): the static tier fails, and is tagged
``pack-build-failed``, when ``charmcraft pack`` fails, where version 2 always
recorded pack ``not_run``.
4 (2026-11-21): pack builds from a fresh git repository, so a charm whose
``override-build`` runs git (alertmanager-k8s) can pack; version 3 failed
those with "not a git repository" whatever the agent wrote.
5 (2026-11-21): ``gold-fetch-refused`` in place of ``gold-fetched`` when the
agent's egress proxy refused every host the gold-naming fetches name.
6 (2026-11-26, after the first scheduled run): the gold-copy check drops the
lines of tests-target's generated acceptance files from both sides (a reused
acceptance test was tagged ``gold-copied``); charmlint is baselined against
the gold (a finding the gold also has isn't a ``lint-regression``); the
``acceptance_tests`` sub-check (an edit to a visible acceptance file fails
static, a reformat is tagged); and the oracle's failure output in
``tiers.oracle.details``."""


def scoring_code_version(charmlint: str) -> str:
    """The version recorded in a run: this code plus the static tier's pinned tools."""
    return f"{SCORING_CODE_VERSION}+charmlint-{charmlint}"
