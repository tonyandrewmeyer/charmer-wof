You are grading a code change to a Juju Kubernetes charm (Python, the `ops` library) made by an AI coding agent: S3 object storage for HedgeDoc's uploads. Grade ONLY the diff; be strict and consistent.

Rubric (integers; total 0-20):
1. correctness (0-8): A `requires: s3` endpoint (interface s3, limit 1) and an `S3Requirer(self, "s3", bucket_name="hedgedoc")`; with credentials, `CMD_IMAGE_UPLOAD_TYPE=s3` overriding config, the access key, secret key and bucket set, region/endpoint/folder (path, default `uploads`) when present; with a relation but no credentials, no configuration is pushed (not a filesystem fallback); credentials_changed/credentials_gone trigger the reconcile. Subtract roughly 2 per substantive defect.
2. ops_idiom_structure (0-6): Fits the reconciler pattern (events funnel into `_reconcile`, status only in collect-status); the CMD_* mapping stays in src/hedgedoc.py free of ops; declares the endpoint and the `charm-libs` entry in charmcraft.yaml; reads connection info from the library rather than caching event data; no dead code or duplication.
3. tests (0-3): Scenario tests cover the credentials case, the pending (no credentials) case and credentials arriving, through State and the Pebble layer.
4. no_collateral_damage (0-3): Existing behaviour and tests intact, no unrelated refactors, no edits to other vendored libs, no weakened lint or test config, no stray files.

Respond with a single JSON object and nothing else, of the form:
{"correctness": n, "ops_idiom_structure": n, "tests": n, "no_collateral_damage": n, "total": n, "notes": "<at most 60 words on the main defects>"}
