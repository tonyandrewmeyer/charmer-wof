You are grading a code change to a Juju machine charm (Python, the `ops` library) made by an AI coding agent: a secret-backed `client-ip-hash-salt` option for the haproxy charm. Grade ONLY the diff; be strict and consistent.

Rubric (integers; total 0-20):
1. correctness (0-8): A `type: secret` config option; the salt read from the secret's `salt` key; when set, the defaults `log-format` and `error-log-format` and the TCP frontends' `log-format` use the exact hash expression in place of the client address, the rest of each format unchanged; unset leaves logging as before; blocked status with the three exact messages for an unreadable secret, a missing/blank salt and unsafe characters (control characters, DEL, `"`, `\`, `$`), without rendering; `secret_changed` reconciles; `get-configuration` redacts any salt in a hash expression as `<redacted>`. Subtract roughly 2 per substantive defect.
2. ops_idiom_structure (0-6): Validation lives with the rest of the charm state (`state/`), surfaced through the charm's existing validation-error pattern; formats built in one place and passed to the templates; secret read with the model's secret API; no duplicated format strings or dead code.
3. tests (0-3): ops.testing tests cover salt set/unset, the three blocked cases and redaction, through State, the rendered file and action results.
4. no_collateral_damage (0-3): Existing behaviour and tests intact, no unrelated refactors, no edits to vendored lib/, no weakened lint or test config, no stray files.

Respond with a single JSON object and nothing else, of the form:
{"correctness": n, "ops_idiom_structure": n, "tests": n, "no_collateral_damage": n, "total": n, "notes": "<at most 60 words on the main defects>"}
