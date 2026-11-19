You are grading a code change to a Juju machine charm (Python, the `ops` library) made by an AI coding agent: a `get-configuration` action for the haproxy charm. Grade ONLY the diff; be strict and consistent.

Rubric (integers; total 0-20):
1. correctness (0-8): The action is declared in charmcraft.yaml and observed; it reads /etc/haproxy/haproxy.cfg with the existing `file_exists`/`read_file` helpers, fails with a "not found" message when it's absent, returns `configuration` (salt-redacted with the existing helper) and `source: disk`, has no side effects; logs the "matches the default configuration" note only when the file equals `render_default_config(CharmState.from_charm(...))`, and the "could not determine" note (still returning the configuration) when the charm state is invalid. Subtract roughly 2 per substantive defect.
2. ops_idiom_structure (0-6): Uses `event.fail`, `event.log` and `event.set_results`; builds the charm state the way the rest of the charm does; reuses existing helpers rather than duplicating them; no dead code.
3. tests (0-3): ops.testing action tests cover the result, the missing file, the default note, the invalid-state note and redaction.
4. no_collateral_damage (0-3): Existing behaviour and tests intact, no unrelated refactors, no edits to vendored lib/, no weakened lint or test config, no stray files.

Respond with a single JSON object and nothing else, of the form:
{"correctness": n, "ops_idiom_structure": n, "tests": n, "no_collateral_damage": n, "total": n, "notes": "<at most 60 words on the main defects>"}
