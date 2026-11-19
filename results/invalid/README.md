# Invalid records

Records that came out of `wof run` but don't measure what they claim to, kept
for the record rather than deleted. They are not in `../results.jsonl`, so the
store reader never sees them.

- `2026-11-10-smoke-gold-visible.jsonl`: the first phase 1 smoke run
  (`hedgedoc-ingress/pi/glm-5-3/none/0`). The gold checkout was readable from
  the agent's shell; GLM-5.3 ran `find / -name ingress.py -path "*traefik*"`,
  found `/home/user/hedgedoc-operator`, diffed the workspace against it and
  copied the gold's lib, `charmcraft.yaml`, `src/charm.py`, tests and docs.
  Static pass and oracle 14/14 are the gold's, not the agent's. Cost $0.729,
  46 turns. The adapter now hides the gold checkout and the task's repo from
  the agent (HARNESS.md, pi, "Isolation").
- `2026-11-17-haproxy-salt-copied-upstream.jsonl`: task set v1's feasibility
  run of `haproxy-client-ip-hash-salt/pi/glm-5-3/none/0`. The gold checkout
  was hidden, but the agent had the network: it searched GitHub's code search
  for `client-ip-hash-salt repo:canonical/haproxy-operator`, which named the
  gold's SHA, then downloaded the charm's files at that SHA from
  raw.githubusercontent.com. Every file it changed is byte-identical to the
  gold. Static pass and oracle 11/11 are the gold's. Cost $0.532, 77 turns.
  This is PLAN §2.3's "public gold on GitHub" case; the record's diff also
  carries `__pycache__` and `.coverage` files, since fixed (the workspace
  excludes build droppings). See PLAN §7, 2026-11-17.
